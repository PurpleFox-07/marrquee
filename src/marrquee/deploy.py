"""The deploy engine: the part that does the real work.

One `DeployManager` per process takes the owner's saved install choices and
turns them into the killer moment - folders built, a readable compose file
written, apps started one at a time, each moving `waiting -> starting ->
done` with a plain-language line at every step. The run happens on a
server-side `asyncio.Task`, independent of any browser: `snapshot()` is
synchronous and always the current truth, and every change is persisted to
disk so a Marrquee restart mid-deploy resumes instead of lying about what
happened.

Every non-deterministic dependency - the Docker engine, the HTTP readiness
probe, the clock, the sleep function, the wiring runner - is an injected
parameter, which is what lets the whole engine be driven end to end in
tests without a real Docker daemon and without a single real second of
waiting.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Protocol, cast

import httpx

from marrquee.catalog import (
    EXISTING_PLEX_APP_ID,
    JELLYFIN_APP_ID,
    PLEX_APP_ID,
    RECYCLARR_APP_ID,
    CatalogApp,
    apps_in_order,
    companions_for,
    get_app,
    require_port,
    riders_of,
    unavailable_reason,
)
from marrquee.compose import (
    StackPlan,
    build_stack_plan,
    clear_vpn_secrets,
    gluetun_config_for,
    write_compose,
    write_vpn_secrets,
)
from marrquee.config import Settings
from marrquee.docker_client import ComposeResult, DockerEngine
from marrquee.hardlinks import HardlinkTrigger
from marrquee.install import with_app_added, with_app_removed
from marrquee.jellyfin import (
    HttpJellyfinServer,
    JellyfinServer,
    ensure_jellyfin_admin,
    jellyfin_base_url,
    load_jellyfin,
)
from marrquee.links import load_links, save_links
from marrquee.login import SavedLogin, load_login, pending_app_ids, record_applied
from marrquee.login_apply import LoginApplier, NoLoginApplier
from marrquee.plex import (
    HttpPlexServer,
    HttpPlexTv,
    PlexServer,
    PlexTv,
    clear_existing_plex,
    clear_plex_claim,
    load_existing_plex,
    load_plex_account,
    plex_base_url,
    plex_host_address,
    save_existing_plex,
    write_plex_claim,
)
from marrquee.qbittorrent import write_qbit_conf
from marrquee.questions import load_answers
from marrquee.recyclarr import (
    RecyclarrTrigger,
    remove_recyclarr_config,
    sync_wanted_after,
    write_recyclarr_config,
)
from marrquee.seerr import (
    HttpSeerrClient,
    SeerrClient,
    ensure_seerr_setup,
    seerr_base_url,
    seerr_sign_in,
    seerr_sign_in_kind,
)
from marrquee.state import InstallState, load_state, save_state, write_json_atomic
from marrquee.storage import (
    FreshnessCheck,
    PathEscapesRoot,
    StorageCheck,
    build_folders,
    check_fresh_start,
    check_storage_root,
    read_marker,
    write_marker,
)
from marrquee.vpn import (
    VPN_APP_ID,
    check_vpn_answers,
    find_provider,
    secret_values,
    tunnel_place_line,
)
from marrquee.vpn_control import (
    GluetunControl,
    NoGluetunControl,
    classify_tunnel,
    looks_like_missing_tun,
)
from marrquee.wiring import NoWiringYet, WiringRunner, WiringStep, WiringStepState
from marrquee.wiring.qbit_client import HttpQbitClient, QbitClient
from marrquee.wiring.steps import app_base_url
from marrquee.without_vpn import clear_without_vpn, without_vpn_confirmed
from marrquee.words import (
    FAILURE_DOCKER_UNREACHABLE,
    FAILURE_EXISTING_PLEX_UNREACHABLE,
    FAILURE_JELLYFIN_NOT_OURS,
    FAILURE_JELLYFIN_PORT_TAKEN,
    FAILURE_JELLYFIN_SETUP_REFUSED,
    FAILURE_PLEX_NOT_CLAIMED,
    FAILURE_PLEX_PORT_TAKEN,
    FAILURE_PLEX_SIGN_IN_NEEDED,
    FAILURE_SEERR_NOT_OURS,
    FAILURE_SEERR_SETUP_REFUSED,
    FAILURE_VPN_NO_TUN,
    JELLYFIN_SERVER_NAME,
    PHASE_HEADLINE_FINALE,
    PHASE_HEADLINE_READY,
    PHASE_HEADLINE_RUNNING,
    PHASE_HEADLINE_WIRING,
    STATUS_CHIP_DONE,
    STATUS_CHIP_ERROR,
    STATUS_CHIP_STARTING,
    STATUS_CHIP_WAITING,
    VPN_LINE_CONNECTING,
    VPN_NOTE_SLOW,
    app_headline_done,
    app_headline_starting,
    app_line_done,
    app_line_error,
    app_line_getting,
    app_line_starting,
    app_line_warming_up,
    app_note_slow_start,
    failure_compose_failed,
    failure_get_failed,
    failure_never_became_ready,
    failure_port_in_use,
    failure_vpn_not_connected,
    failure_vpn_refused,
    failure_vpn_settings_refused,
    hub_cancel_failed,
    hub_line_connecting,
    hub_login_line_putting,
    hub_login_line_restarting,
    refusal_name_clash,
    refusal_not_a_folder,
    refusal_not_shared,
    refusal_not_writable,
    refusal_path_missing,
    refusal_populated_target,
    refusal_system_path,
    wiring_finale_note,
)

logger = logging.getLogger(__name__)

AppState = Literal["waiting", "starting", "done", "error"]
DeployPhase = Literal["ready", "running", "wiring", "finale", "error"]
FailureCode = Literal[
    "docker_unreachable",
    "storage_refused",
    "name_clash",
    "image_download_failed",
    "compose_failed",
    "never_became_ready",
    "port_in_use",
    "vpn_refused",
    "vpn_settings_refused",
    "vpn_not_connected",
    "vpn_no_tun",
    "plex_sign_in_needed",
    "plex_not_claimed",
    "plex_port_taken",
    "jellyfin_port_taken",
    "jellyfin_not_ours",
    "jellyfin_setup_refused",
    "existing_plex_unreachable",
    "seerr_not_ours",
    "seerr_setup_refused",
]

# An add's own tiny state machine - never "done": once wiring finishes, the
# app moves into `DeploySnapshot.apps` and `adding` goes back to `None`,
# exactly the way a full deploy never keeps a `done` app around as
# `adding` either.
AddState = Literal["starting", "wiring", "error"]
# "add" is a brand new app; "reconnect" re-runs only the wiring steps for an
# app that is already `done` in `apps` - it never touches Docker at all.
# "change_vpn" removes and recreates an already-installed Gluetun from
# freshly saved secrets (a login-only change never shows up in compose.yaml,
# so a plain recreate would keep the old login forever); "restore" is the
# "Keep running without VPN" recovery from a failed "add" or "change_vpn" -
# it brings the mover(s) a failed move stopped back up, never touching a
# VPN app at all.
AddPurpose = Literal["add", "reconnect", "change_vpn", "restore"]
AddStart = Literal[
    "started", "busy", "not_ready", "no_login", "unknown_app", "already_installed", "unavailable"
]
# The login run's own start-refusal table - "choose", "change" and "retry"
# all share it, since all three ultimately call `apply_login()`.
LoginStart = Literal["started", "busy", "not_ready", "no_login", "nothing_to_do"]
# `disconnect`'s own refusal table. "not_disconnectable" is a managed app
# (every real, deployed app) - only the owner's own, connected-but-never-
# deployed Plex can ever be disconnected. "needed" is another already-
# installed app whose own rule would stop holding the moment this one goes.
DisconnectOutcome = Literal["done", "busy", "not_installed", "not_disconnectable", "needed"]

_DEPLOY_FILE_NAME = "deploy.json"
_DIAGNOSTICS_FILE_NAME = "last-failure.txt"
_REDACTED_PLACEHOLDER = "<redacted-api-key>"
_REDACTED_PASSWORD_PLACEHOLDER = "<redacted-password>"
_REDACTED_VPN_PLACEHOLDER = "<redacted-vpn-login>"
_REDACTED_PLEX_TOKEN_PLACEHOLDER = "<redacted-plex-token>"
_REDACTED_PLEX_CLAIM_PLACEHOLDER = "<redacted-plex-claim>"
_REDACTED_JELLYFIN_PLACEHOLDER = "<redacted-jellyfin-key>"
# A claim code's own shape (see `plex.write_plex_claim`) - matched by value
# would miss a code this process never wrote itself (an earlier, unrelated
# run's leftover in an old log line), so every diagnostics write is swept
# for the shape too, not only the current claim.
_PLEX_CLAIM_PATTERN = re.compile(r"claim-[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class AppProgress:
    """One app's place in the deploy, in words a beginner understands.

    Carries the app's published port only - the snapshot is built
    server-side with no browser request attached, so no full address in it
    can ever be right for a second device on the network. A page builds the
    clickable link itself, per request, from the host it was reached on.
    """

    app_id: str
    name: str
    state: AppState
    chip: str
    line: str
    note: str | None
    port: int | None


@dataclass(frozen=True)
class Failure:
    """A real failure, split into what the owner sees and what a developer needs.

    `technical` is written to the log and the diagnostics file only, and
    never becomes part of a rendered field - the same split the rest of this
    codebase uses for `DockerStatus.detail` and `StorageCheck.detail`.
    """

    code: FailureCode
    headline: str
    what_to_do: str
    technical: str


@dataclass(frozen=True)
class AppAdd:
    """One app being added (or reconnected) to an already-finale deploy.

    Lives entirely inside `DeploySnapshot.adding`, never in `apps` - the Hub
    builds its posters from `apps`, so a half-added app stays invisible to
    every reader of that list until it is genuinely `done`. `compose_ran`
    is what lets Cancel tell "Docker was actually asked to create this
    container" apart from "we refused before ever touching Docker" (a name
    clash) - removing a container in the second case would delete someone
    else's.
    """

    app_id: str
    purpose: AddPurpose
    state: AddState
    line: str
    note: str | None
    failure: Failure | None
    wiring: tuple[WiringStep, ...]
    compose_ran: bool
    started_at: str
    # The companion(s) this add brought along with it (Gluetun, for
    # qBittorrent) - set once, by `add_app`/`retry_add`, before the run
    # ever starts, so a resumed add reads the exact same set rather than
    # re-deriving it against an install that may have grown since.
    with_apps: tuple[str, ...] = ()
    # The already-INSTALLED app(s) this run stops and brings back behind
    # (or, for "restore", back OFF) the VPN - qBittorrent, for an
    # `app_id="gluetun"` add or a `change_vpn`. Set once, before the run
    # ever starts, for the same resume-reads-the-same-set reason as
    # `with_apps` above. Empty for every plain app add and every reconnect.
    moves: tuple[str, ...] = ()


@dataclass(frozen=True)
class WiringGap:
    """One already-installed app whose wiring didn't fully finish.

    `app_id` is the app whose add or reconnect ran the steps that produced
    these lines - the tile that gets the amber note and the "Connect
    again" button.
    """

    app_id: str
    failed_lines: tuple[str, ...]


@dataclass(frozen=True)
class Disconnect:
    """The result of `DeployManager.disconnect` - never raises, and every
    refusal happens before anything on disk is touched.

    `needed_by` is set only for `outcome == "needed"`: the name of the
    other already-installed app whose own rule needs this one to stay.
    """

    outcome: DisconnectOutcome
    needed_by: str | None


@dataclass(frozen=True)
class DeploySnapshot:
    """The whole truth about the current (or most recent) deploy, right now."""

    run_id: str
    phase: DeployPhase
    apps: tuple[AppProgress, ...]
    headline: str
    detail: str | None
    failure: Failure | None
    started_at: str | None
    finished_at: str | None
    wiring: tuple[WiringStep, ...] = ()
    adding: AppAdd | None = None
    wiring_gaps: tuple[WiringGap, ...] = ()


class ReadinessProbe(Protocol):
    """Answers "has this app finished booting and accepted our key?"."""

    async def check(self, host: str, port: int, api_base: str, api_key: str) -> bool: ...


class HttpReadinessProbe:
    """Probes `GET http://<host>:<port>/<api_base>/system/status` with our key.

    `done` means this returns 200 - not merely that the container is
    running. A connection-level failure (refused, timed out) is the normal
    state for the first several seconds of a fresh container, so it is
    treated as "not ready yet" rather than as an error; only a real answer
    decides readiness.
    """

    def __init__(
        self, *, timeout: float = 3.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def check(self, host: str, port: int, api_base: str, api_key: str) -> bool:
        url = f"http://{host}:{port}/{api_base}/system/status"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.get(url, headers={"X-Api-Key": api_key})
        except httpx.HTTPError:
            return False
        return response.status_code == 200


class FakeReadinessProbe:
    """A scriptable ReadinessProbe for tests - no network involved.

    `responses` holds a queue of answers per `(host, port)`, drained in
    order; once a queue is empty (or was never given), `default` answers
    every further call - the shape that lets a test say "not ready a few
    times, then ready" without any real waiting.
    """

    def __init__(
        self,
        *,
        responses: Mapping[tuple[str, int], Iterable[bool]] | None = None,
        default: bool = True,
    ) -> None:
        self._queues: dict[tuple[str, int], list[bool]] = {
            key: list(values) for key, values in (responses or {}).items()
        }
        self._default = default
        self.calls: list[tuple[str, int, str, str]] = []

    async def check(self, host: str, port: int, api_base: str, api_key: str) -> bool:
        self.calls.append((host, port, api_base, api_key))
        queue = self._queues.get((host, port))
        if queue:
            return queue.pop(0)
        return self._default


class DeployManager:
    """Runs, watches and remembers exactly one deploy per Marrquee process.

    The run itself lives entirely inside `_run` and its private helpers;
    everything else here is bookkeeping - starting the background task,
    persisting every change, and serving the truth to however many
    subscribers are watching, without ever letting a slow or vanished one
    stall the run.
    """

    # The mockup's own hiccup beat sets the expectation that slowness is
    # normal, and a first arr start on a NAS with a cold database routinely
    # takes longer than a minute - 300s means a timeout is a genuine problem.
    POLL_INTERVAL_SECONDS: ClassVar[float] = 2.0
    REASSURANCE_AFTER_SECONDS: ClassVar[float] = 45.0
    NEVER_READY_AFTER_SECONDS: ClassVar[float] = 300.0
    # The tunnel loop's own timeout - shorter than the arr loop's, because a
    # tunnel that hasn't come up after two minutes needs a plain sentence,
    # not a slow-database benefit of the doubt. The grace period is how long
    # a healthy, running tunnel is allowed to report no address at all
    # before Marrquee stops waiting for one and calls it proven anyway (a
    # provider that answers `/v1/publicip/ip` slowly, say).
    TUNNEL_NEVER_UP_AFTER_SECONDS: ClassVar[float] = 120.0
    TUNNEL_PLACE_GRACE_SECONDS: ClassVar[float] = 20.0
    # `init-plex-claim` briefly starts PMS before it ever claims, so a lone
    # unclaimed `/identity` reply is normal on a healthy first start, not a
    # verdict - only a logged claim failure or this many continuous seconds
    # of it means the code genuinely didn't take.
    PLEX_UNCLAIMED_GRACE_SECONDS: ClassVar[float] = 90.0

    def __init__(
        self,
        settings: Settings,
        engine: DockerEngine,
        *,
        probe: ReadinessProbe = HttpReadinessProbe(),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wiring: WiringRunner = NoWiringYet(),
        login: LoginApplier = NoLoginApplier(),
        vpn: GluetunControl = NoGluetunControl(),
        qbit: QbitClient = HttpQbitClient(),
        hardlinks: HardlinkTrigger | None = None,
        recyclarr: RecyclarrTrigger | None = None,
        plex_tv: PlexTv | None = None,
        plex_server: PlexServer | None = None,
        jellyfin: JellyfinServer | None = None,
        seerr: SeerrClient | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._probe = probe
        self._clock = clock
        self._sleep = sleep
        self._wiring = wiring
        self._login = login
        self._vpn = vpn
        self._qbit = qbit
        self._hardlinks = hardlinks
        self._recyclarr = recyclarr
        self._plex_tv = plex_tv if plex_tv is not None else HttpPlexTv()
        self._plex_server = plex_server if plex_server is not None else HttpPlexServer()
        self._jellyfin = jellyfin if jellyfin is not None else HttpJellyfinServer()
        self._seerr = seerr if seerr is not None else HttpSeerrClient()
        self._task: asyncio.Task[None] | None = None
        # The login run's own in-memory progress line - never persisted and
        # never resumed after a restart (the pending names plus Try again
        # already cover that case), the same "server truth, no browser
        # required" idea as `snapshot()` itself, just not part of it.
        self._login_progress: str | None = None
        self._subscribers: list[asyncio.Queue[DeploySnapshot]] = []
        self._deploy_file = settings.config_dir / _DEPLOY_FILE_NAME
        self._diagnostics_file = settings.config_dir / _DIAGNOSTICS_FILE_NAME
        # Read back whatever the last process persisted - this is what lets
        # a NAS restart mid-deploy still answer "finale" (or "running", so
        # `resume_if_interrupted` knows to re-enter it) instead of lying
        # that nothing ever happened.
        self._snapshot = self._load_persisted_snapshot() or _resting_snapshot()

    # --- The truth, right now -------------------------------------------

    def snapshot(self) -> DeploySnapshot:
        """The current truth. Synchronous, and always accurate.

        While nothing has ever run, this reflects the owner's saved choices
        (every chosen app, `waiting`) rather than an empty screen - reading
        it fresh on every call is what keeps it accurate even if the owner
        changes their install choices before ever pressing deploy.
        """
        if self._snapshot.phase == "ready":
            return self._ready_snapshot()
        return self._snapshot

    def _ready_snapshot(self) -> DeploySnapshot:
        install = load_state(self._settings.config_dir)
        apps = (
            ()
            if install is None
            else tuple(_waiting_progress(app) for app in apps_in_order(install.app_ids))
        )
        return replace(self._snapshot, apps=apps)

    # --- Starting and resuming -------------------------------------------

    def start(self) -> DeploySnapshot:
        """Start a deploy if none is running; otherwise, hand back the one already going.

        A NEW run clears the diagnostics file first, so "Last problem" on
        the Diagnostics page can only ever show the most recent run's own
        problem, never one an earlier, unrelated deploy left behind.
        `resume_if_interrupted` never does this - a restart mid-deploy is
        the same run continuing, and its evidence should survive it.
        """
        if self._is_running():
            return self.snapshot()
        install = load_state(self._settings.config_dir)
        if install is None:
            return self.snapshot()
        self._clear_diagnostics()
        self._task = asyncio.create_task(self._run(install))
        return self.snapshot()

    def _clear_diagnostics(self) -> None:
        try:
            self._diagnostics_file.unlink(missing_ok=True)
        except OSError:
            pass  # a locked or otherwise unremovable file must never stop a deploy

    async def resume_if_interrupted(self) -> DeploySnapshot:
        """Re-enter a deploy that was still going when this process last stopped.

        Every step the run takes is idempotent (folders, marker, compose
        file, `compose up --no-recreate`, network connect - joined again per
        app, which also self-heals a Marrquee container recreated by this
        very restart, no longer being on the network at all), so re-running
        the whole sequence is a repeat, not a rollback - reporting `error`
        here instead would tell the owner their deploy failed while their
        containers are visibly fine.
        """
        if self._is_running():
            return self.snapshot()
        if self._snapshot.phase == "finale":
            return self._resume_add_if_interrupted()
        if self._snapshot.phase not in ("running", "wiring"):
            return self.snapshot()
        install = load_state(self._settings.config_dir)
        if install is None:
            return self.snapshot()
        self._task = asyncio.create_task(self._run(install))
        return self.snapshot()

    def _resume_add_if_interrupted(self) -> DeploySnapshot:
        """A `finale` deploy whose `adding` was still `starting`/`wiring` when
        this process last stopped - the add-path equivalent of the branch
        just above.

        An `adding` already `error` needs an owner's "Try again", not an
        automatic resume - the same reason a full deploy's own `error`
        phase is never auto-resumed either.
        """
        adding = self._snapshot.adding
        if adding is None or adding.state not in ("starting", "wiring"):
            return self.snapshot()
        install = load_state(self._settings.config_dir)
        if install is None:
            return self.snapshot()
        try:
            app = get_app(adding.app_id)
        except KeyError:
            return self.snapshot()
        if adding.purpose == "add":
            # Idempotent: a mover's stop/remove (or its own bring-up, or
            # Gluetun's) against a container a real daemon already
            # actioned before the restart just repeats, never rolls back.
            self._task = asyncio.create_task(self._run_add(app, install))
        elif adding.purpose == "change_vpn":
            self._task = asyncio.create_task(self._run_change_vpn())
        elif adding.purpose == "restore":
            self._task = asyncio.create_task(self._run_restore())
        else:
            self._task = asyncio.create_task(self._run_reconnect(app, install))
        return self.snapshot()

    def _is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_busy(self) -> bool:
        """Whether a run (a full deploy, an add, a retry, a reconnect or a
        login run) is actively going right now - the Hub's own "one thing at
        a time" flag, as opposed to `adding is not None`, which also stays
        true while a failed add just sits there waiting for "Try again" or
        "Cancel".
        """
        return self._is_running()

    def login_progress(self) -> str | None:
        """The login run's own current line ("Putting your login on
        Sonarr…"), or `None` while no login run is going.

        In memory only - a restart mid-run loses this line, but never the
        truth: `login.json`'s own `applied` map is what the Hub reads to
        name any app still pending, with its own "Try again".
        """
        return self._login_progress

    def return_to_ready(self) -> DeploySnapshot:
        """Bring back the Deploy button after new choices are saved.

        Called once, from the wizard's own success path, right after new
        choices land on disk - the persisted "finale" or "error" from an
        earlier deploy would otherwise send the owner straight back to the
        old finale with no way to press Deploy again. A run still in
        flight is left alone: saving new choices in a second tab must
        never interrupt one already going.
        """
        if self._is_running():
            return self.snapshot()
        if self._snapshot.phase in ("finale", "error"):
            self._emit(_resting_snapshot())
        return self.snapshot()

    # --- Adding, retrying, cancelling and reconnecting one app -----------

    def add_app(self, app_id: str) -> AddStart:
        """Start adding one app to an already-finale deploy - never a second
        full deploy.

        Synchronous, and refuses before ever touching Docker: only one add
        (or one waiting failed add) is allowed at a time, so a second
        `add_app` while the first is still going, or still sitting there
        failed, is refused as `"busy"` rather than silently queued or
        silently ignored. Nothing awaits between the refusal checks and
        `create_task` - a second call arriving before the first `await`
        point inside the new task could otherwise slip past `adding is not
        None` and start a second add for real.
        """
        install = load_state(self._settings.config_dir)
        snapshot = self.snapshot()
        if install is None or snapshot.phase != "finale":
            return "not_ready"
        if load_login(self._settings.config_dir).login is None:
            return "no_login"
        if self._is_running() or snapshot.adding is not None:
            return "busy"
        try:
            app = get_app(app_id)
        except KeyError:
            return "unknown_app"
        if any(progress.app_id == app_id for progress in snapshot.apps):
            return "already_installed"
        present_ids = tuple(progress.app_id for progress in snapshot.apps)
        if unavailable_reason(app, present_ids) is not None:
            return "unavailable"

        # Gluetun already installed (a separate, earlier add) never rides
        # along a second time here - `_run_add_steps` instead removes and
        # re-brings it up on its own, so its key/secrets stay current for
        # the new rider. Only a genuinely absent companion is brought along.
        via_already_installed = app.network_via is not None and app.network_via in present_ids
        companions = (
            ()
            if via_already_installed
            else companions_for(
                app_id, present_ids, without_vpn=without_vpn_confirmed(self._settings.config_dir)
            )
        )
        # Adding the VPN itself moves every already-installed rider behind
        # it - the one case this add stops and brings BACK an already
        # running app rather than only creating new ones.
        moves = (
            tuple(rider.id for rider in riders_of(app_id, present_ids)) if app.kind == "vpn" else ()
        )

        started_at = _now_iso()
        adding = AppAdd(
            app_id=app_id,
            purpose="add",
            state="starting",
            line=app_line_starting(app.name),
            note=None,
            failure=None,
            wiring=(),
            compose_ran=False,
            started_at=started_at,
            with_apps=companions,
            moves=moves,
        )
        self._emit(replace(snapshot, adding=adding))
        self._task = asyncio.create_task(self._run_add(app, install))
        return "started"

    def retry_add(self) -> AddStart:
        """Re-run a failed add, change or restore from scratch - the
        add-path "press Deploy again", widened to the other two run kinds
        that can also end in `state="error"`.
        """
        if self._is_running():
            return "busy"
        current = self._snapshot.adding
        if (
            current is None
            or current.state != "error"
            or current.purpose
            not in (
                "add",
                "change_vpn",
                "restore",
            )
        ):
            return "busy"
        install = load_state(self._settings.config_dir)
        if install is None:
            return "not_ready"
        app = get_app(current.app_id)

        started_at = _now_iso()
        adding = AppAdd(
            app_id=app.id,
            purpose=current.purpose,
            state="starting",
            line=app_line_starting(app.name),
            note=None,
            failure=None,
            wiring=(),
            compose_ran=current.compose_ran,
            started_at=started_at,
            with_apps=current.with_apps,
            moves=current.moves,
        )
        self._emit(self._replace_finale(adding=adding))
        if current.purpose == "change_vpn":
            self._task = asyncio.create_task(self._run_change_vpn())
        elif current.purpose == "restore":
            self._task = asyncio.create_task(self._run_restore())
        else:
            self._task = asyncio.create_task(self._run_add(app, install))
        return "started"

    async def cancel_add(self) -> bool:
        """Give up on a failed add: remove the container it created (if any),
        drop the app from install.json, and put it back in the "+" list.

        Only ever removes a container `compose_ran` says WE asked Docker to
        create - a name-clash refusal never got that far, so Cancel after
        one never touches a container it didn't create. Never deletes a
        folder: the app's API key and every folder Marrquee built for it
        are left exactly where they are, ready for a later re-add.

        Refuses for a failed `reconnect` too, even though its `AppAdd`
        always carries `compose_ran=True` (a reconnect never calls Docker
        at all, so that flag alone can't tell "we created this" apart from
        "this was already installed and running"). A failed reconnect is
        already-installed, working app - Cancel must never remove it;
        `reconnect(app_id)` is the only recovery this offers. A failed
        `change_vpn` or `restore` refuses too - the VPN was already the
        one installed (there is nothing to "put back in the + list"); Try
        again is the only recovery those two offer.

        When this add brought a VPN's rider(s) along (`current.moves`),
        cancelling is "Keep running without VPN": the confirmation must
        already be on disk (it is what lets the rewritten compose file put
        qBittorrent back on its own network instead of raising), and
        instead of leaving `adding=None`, a `restore` run brings the
        rider(s) back up.
        """
        current = self._snapshot.adding
        if current is None or current.state != "error" or current.purpose != "add":
            return False

        if current.moves and not without_vpn_confirmed(self._settings.config_dir):
            failed = replace(current, line=hub_cancel_failed(get_app(current.app_id).name))
            self._emit(self._replace_finale(adding=failed))
            return False

        # Every app this add brought along, in REVERSE catalog order
        # (qBittorrent, then Gluetun) - a rider's container has to go
        # before the network namespace it shares disappears with its
        # companion's.
        brought = apps_in_order((*current.with_apps, current.app_id))
        if current.compose_ran:
            for removed_app in reversed(brought):
                if not removed_app.managed:
                    # Never had a container - not even a stale, copied-forward
                    # `compose_ran` flag says otherwise.
                    continue
                result = await self._engine.remove_container(removed_app.id)
                if not result.ok:
                    failed = replace(current, line=hub_cancel_failed(removed_app.name))
                    self._emit(self._replace_finale(adding=failed))
                    return False

        install = load_state(self._settings.config_dir)
        if install is not None:
            shrunk = install
            for removed_app in brought:
                shrunk = with_app_removed(shrunk, removed_app.id)
            save_state(self._settings.config_dir, shrunk)
            if shrunk.storage_root is not None:
                root = PurePosixPath(shrunk.storage_root)
                write_marker(self._settings, root, shrunk.app_ids, shrunk.puid, shrunk.pgid)
                # A DIFFERENT app's failed add is being cancelled while
                # Gluetun (already installed) has since been left with
                # unusable saved answers - rewriting compose can't succeed,
                # but the cancel itself must still complete rather than
                # 500: `build_gluetun_config` raises with no value in its
                # message, so logging it directly is already safe.
                try:
                    write_compose(self._settings, self._stack_plan(shrunk))
                except ValueError as error:
                    logger.error("cancel: could not rewrite compose.yaml: %s", error)
                if any(removed_app.kind == "vpn" for removed_app in brought):
                    clear_vpn_secrets(self._settings, root)
                if any(removed_app.kind == "sync" for removed_app in brought):
                    remove_recyclarr_config(self._settings, root)
                if any(removed_app.id == PLEX_APP_ID for removed_app in brought):
                    # Jellyfin's own cancel keeps its settings folder (and so
                    # its key) exactly where a full deploy or the next add
                    # would find it - only Plex's short-lived claim code
                    # needs clearing here.
                    clear_plex_claim(self._settings, root)
            if any(removed_app.id == EXISTING_PLEX_APP_ID for removed_app in brought):
                # Never inside the `storage_root` block above: the record
                # lives in config_dir, not on the storage root, so it must
                # go even for a cancel that never saved a storage root at
                # all (impossible for existing-plex itself, but not for a
                # companion sharing this same cancel).
                clear_existing_plex(self._settings.config_dir)

        if current.moves:
            first_mover = get_app(current.moves[0])
            restore_adding = AppAdd(
                app_id=first_mover.id,
                purpose="restore",
                state="starting",
                line=app_line_starting(first_mover.name),
                note=None,
                failure=None,
                wiring=(),
                compose_ran=True,
                started_at=_now_iso(),
                with_apps=(),
                moves=current.moves,
            )
            self._emit(self._replace_finale(adding=restore_adding))
            self._task = asyncio.create_task(self._run_restore())
            return True

        self._emit(self._replace_finale(adding=None))
        return True

    def reconnect(self, app_id: str) -> AddStart:
        """Re-run only one already-installed app's wiring steps.

        The same "press it again" idea a failed full deploy already offers,
        narrowed to one app: nothing about the app itself is touched, only
        the connections its wiring steps make - so an app that is already
        fully wired is never at risk of losing anything by being
        reconnected again.
        """
        install = load_state(self._settings.config_dir)
        snapshot = self.snapshot()
        if install is None or snapshot.phase != "finale":
            return "not_ready"
        if self._is_running() or snapshot.adding is not None:
            return "busy"
        if not any(progress.app_id == app_id for progress in snapshot.apps):
            return "unknown_app"
        app = get_app(app_id)

        started_at = _now_iso()
        adding = AppAdd(
            app_id=app_id,
            purpose="reconnect",
            state="wiring",
            line=hub_line_connecting(app.name),
            note=None,
            failure=None,
            wiring=(),
            compose_ran=True,
            started_at=started_at,
        )
        self._emit(replace(snapshot, adding=adding))
        self._task = asyncio.create_task(self._run_reconnect(app, install))
        return "started"

    def change_vpn(self) -> AddStart:
        """Start replacing an already-connected Gluetun's login: stop and
        remove its rider(s), remove Gluetun itself, create it again from
        freshly saved secrets, prove the tunnel, then bring the rider(s)
        back.

        Synchronous, and refuses before ever touching Docker - the same
        "nothing can slip between the checks and `create_task`" shape
        `add_app` already uses. The route calling this saves the new
        answers itself, right after this returns `"started"` and before
        its own next `await` - by the time `_run_change_vpn` reaches its
        first `load_answers` call, the new answers are already on disk.
        """
        install = load_state(self._settings.config_dir)
        snapshot = self.snapshot()
        if install is None or snapshot.phase != "finale":
            return "not_ready"
        if self._is_running() or snapshot.adding is not None:
            return "busy"
        present_ids = tuple(progress.app_id for progress in snapshot.apps)
        if VPN_APP_ID not in present_ids:
            return "unknown_app"
        app = get_app(VPN_APP_ID)
        moves = tuple(rider.id for rider in riders_of(VPN_APP_ID, present_ids))

        started_at = _now_iso()
        adding = AppAdd(
            app_id=VPN_APP_ID,
            purpose="change_vpn",
            state="starting",
            line=app_line_starting(app.name),
            note=None,
            failure=None,
            wiring=(),
            compose_ran=True,
            started_at=started_at,
            with_apps=(),
            moves=moves,
        )
        self._emit(replace(snapshot, adding=adding))
        self._task = asyncio.create_task(self._run_change_vpn())
        return "started"

    # --- Disconnecting an unmanaged app (the owner's own Plex) -------------

    async def disconnect(self, app_id: str) -> Disconnect:
        """Take an unmanaged app off the Hub - only ever the owner's own
        Plex, never a real, deployed one: no container to remove, no Plex
        call, nothing on the daemon touched at all. Every refusal is
        checked before the first `await`, the same nothing-can-slip-in-
        between shape `add_app` already uses.
        """
        if self._is_running() or self._snapshot.adding is not None:
            return Disconnect("busy", None)

        snapshot = self.snapshot()
        present_ids = tuple(progress.app_id for progress in snapshot.apps)
        if app_id not in present_ids:
            return Disconnect("not_installed", None)

        app = get_app(app_id)
        if app.managed:
            return Disconnect("not_disconnectable", None)

        # Whether some OTHER already-installed app's own rule would stop
        # holding the moment this one is gone - Seerr needing a media
        # server present is the case this protects.
        remaining_ids = tuple(pid for pid in present_ids if pid != app_id)
        for other in apps_in_order(remaining_ids):
            if unavailable_reason(other, remaining_ids) is not None:
                return Disconnect("needed", other.name)

        install = load_state(self._settings.config_dir)
        if install is None:
            return Disconnect("not_installed", None)

        shrunk = with_app_removed(install, app_id)
        save_state(self._settings.config_dir, shrunk)
        if shrunk.storage_root is not None:
            root = PurePosixPath(shrunk.storage_root)
            write_marker(self._settings, root, shrunk.app_ids, shrunk.puid, shrunk.pgid)
        clear_existing_plex(self._settings.config_dir)

        new_apps = tuple(progress for progress in snapshot.apps if progress.app_id != app_id)
        new_gaps = tuple(gap for gap in snapshot.wiring_gaps if gap.app_id != app_id)
        await self._publish(self._replace_finale(adding=None, apps=new_apps, wiring_gaps=new_gaps))
        return Disconnect("done", None)

    # --- Choosing, changing or retrying the one saved login --------------

    def apply_login(self) -> LoginStart:
        """Start the login run: choose, change and retry all end up here.

        Synchronous, and refuses in order before ever awaiting anything -
        the same "nothing can slip between the checks and `create_task`"
        shape `add_app` already uses. `install_state`/`login.json` are read
        fresh on every call, so a login saved by one request is always seen
        by the very next.
        """
        install = load_state(self._settings.config_dir)
        snapshot = self.snapshot()
        if install is None or snapshot.phase != "finale":
            return "not_ready"
        if self._is_running():
            return "busy"
        record = load_login(self._settings.config_dir)
        if record.login is None:
            return "no_login"
        installed_ids = tuple(progress.app_id for progress in snapshot.apps)
        targets = pending_app_ids(record, installed_ids)
        if not targets:
            return "nothing_to_do"
        self._task = asyncio.create_task(self._run_login(install, record.login, targets))
        return "started"

    def _current_adding(self) -> AppAdd | None:
        return self._snapshot.adding

    def _replace_finale(
        self,
        *,
        adding: AppAdd | None,
        apps: tuple[AppProgress, ...] | None = None,
        wiring_gaps: tuple[WiringGap, ...] | None = None,
    ) -> DeploySnapshot:
        """The current snapshot with only `adding` (and optionally `apps` /
        `wiring_gaps`) swapped - `phase`, `headline`, `detail`, `failure`,
        `wiring`, `started_at` and `finished_at` all stay exactly what the
        finale deploy already set them to. This is what keeps every
        snapshot emitted during an add still reading `phase == "finale"` -
        the Hub never bounces to /deploy while one is running.
        """
        current = self._snapshot
        return replace(
            current,
            adding=adding,
            apps=current.apps if apps is None else apps,
            wiring_gaps=current.wiring_gaps if wiring_gaps is None else wiring_gaps,
        )

    def _gaps_with(self, app_id: str, failed_lines: tuple[str, ...]) -> tuple[WiringGap, ...]:
        """`wiring_gaps` with `app_id`'s own entry replaced, added, or
        removed - a clean reconnect drops a gap it just closed instead of
        leaving a stale one behind.
        """
        remaining = tuple(gap for gap in self._snapshot.wiring_gaps if gap.app_id != app_id)
        if not failed_lines:
            return remaining
        return (*remaining, WiringGap(app_id=app_id, failed_lines=failed_lines))

    # --- Subscribing -------------------------------------------------------

    async def subscribe(self) -> AsyncIterator[DeploySnapshot]:
        """Yield the current snapshot immediately, then one per change.

        Delivery is best-effort: a subscriber that stops reading fills its
        own one-slot queue and is dropped rather than allowed to make
        `_emit` block the run - the deploy has to keep going independent of
        any browser connection, closed tab or otherwise.
        """
        queue: asyncio.Queue[DeploySnapshot] = asyncio.Queue(maxsize=1)
        queue.put_nowait(self.snapshot())
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    # --- Persistence and fan-out --------------------------------------------

    def _emit(self, snapshot: DeploySnapshot) -> None:
        self._snapshot = snapshot
        write_json_atomic(self._deploy_file, dataclasses.asdict(snapshot))
        still_listening = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(snapshot)
                still_listening.append(queue)
            except asyncio.QueueFull:
                pass  # a slow subscriber is dropped, never allowed to block the run
        self._subscribers = still_listening

    async def _publish(self, snapshot: DeploySnapshot) -> None:
        """Emit, then yield once - so a subscriber gets a real chance to see it
        before the run races on to the next state.
        """
        self._emit(snapshot)
        await asyncio.sleep(0)

    def _load_persisted_snapshot(self) -> DeploySnapshot | None:
        try:
            raw = self._deploy_file.read_text()
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        try:
            return _snapshot_from_payload(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def _stack_plan(self, install: InstallState) -> StackPlan:
        """The one place `build_stack_plan` is ever called from: threads
        the saved answers AND the break-glass confirmation through on
        every call, so a compose file can never be written by a caller
        that forgot either one.
        """
        answers = load_answers(self._settings.config_dir)
        return build_stack_plan(
            install, answers, without_vpn=without_vpn_confirmed(self._settings.config_dir)
        )

    # --- The run itself ------------------------------------------------------

    async def _run(self, install: InstallState) -> None:
        run_id = uuid.uuid4().hex
        started_at = _now_iso()
        catalog_apps = apps_in_order(install.app_ids)
        progresses = [_waiting_progress(app) for app in catalog_apps]

        try:
            await self._run_steps(install, run_id, started_at, catalog_apps, progresses)
        except Exception as error:  # the background task must never die silently
            logger.exception("deploy engine crashed unexpectedly")
            headline, what_to_do = _split_failure_text(failure_compose_failed("Marrquee"))
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
            )

    async def _run_steps(
        self,
        install: InstallState,
        run_id: str,
        started_at: str,
        catalog_apps: tuple[CatalogApp, ...],
        progresses: list[AppProgress],
    ) -> None:
        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="running",
                apps=tuple(progresses),
                headline=PHASE_HEADLINE_RUNNING,
                detail=None,
                failure=None,
                started_at=started_at,
                finished_at=None,
                wiring=(),
            )
        )

        docker_status = await self._engine.status()
        if not docker_status.connected:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _docker_unreachable_failure(docker_status.detail or "Docker did not answer"),
            )
            return

        if install.storage_root is None:
            headline, what_to_do = _split_failure_text(
                refusal_path_missing("(no storage folder chosen)")
            )
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                Failure(
                    code="storage_refused",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical="no storage_root was ever saved",
                ),
            )
            return

        storage_check = check_storage_root(self._settings, install.storage_root)
        if not storage_check.ok:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _storage_failure(storage_check, install.storage_root),
            )
            return

        root = PurePosixPath(install.storage_root)
        freshness = check_fresh_start(self._settings, root, install.app_ids)
        if not freshness.ok:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _freshness_failure(freshness, install.storage_root),
            )
            return

        clash = await self._find_name_clash(install, root, catalog_apps)
        if clash is not None:
            await self._fail(run_id, started_at, progresses, install, clash)
            return

        build_folders(self._settings, root, install.app_ids, install.puid, install.pgid)
        write_marker(self._settings, root, install.app_ids, install.puid, install.pgid)
        answers = load_answers(self._settings.config_dir)
        vpn_failure = _vpn_settings_failure(install.app_ids, answers)
        if vpn_failure is not None:
            await self._fail(run_id, started_at, progresses, install, vpn_failure)
            return
        plan = self._stack_plan(install)
        compose_path = write_compose(self._settings, plan)

        # Marrquee has no label on the stack's own compose project, so this
        # fallback chain is the only way to identify its own container -
        # checked once, up front, since a missing id means nothing could
        # ever join the network later either, regardless of anything else.
        self_id = await self._engine.self_container_id()
        if self_id is None:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _docker_unreachable_failure("could not identify Marrquee's own container"),
            )
            return

        for index, app in enumerate(catalog_apps):
            report = self._full_deploy_reporter(progresses, index, app, run_id, started_at)
            failure = await self._bring_up_app(
                app, install, root, compose_path, plan.network, self_id, report
            )
            if failure is not None:
                await self._fail(run_id, started_at, progresses, install, failure)
                return

        # Every app is up - put the saved login on each one now, in catalog
        # order, before wiring ever runs. A login failure never fails the
        # deploy itself: the apps are already up and done, and the Hub names
        # any app still missing the login with its own "Try again".
        for app in catalog_apps:
            await self._put_login(app, install)

        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="wiring",
                apps=tuple(progresses),
                headline=PHASE_HEADLINE_WIRING,
                detail=None,
                failure=None,
                started_at=started_at,
                finished_at=None,
                wiring=(),
            )
        )

        # Keyed by `index` rather than appended, so a step that re-emits
        # `running` (a reassurance note) or moves from `running` to a
        # terminal state replaces its own row instead of leaving a stale one
        # behind - the screen (and the finale) only ever sees the latest
        # frame for each step.
        wiring_rows: dict[int, WiringStep] = {}

        def collect_wiring_step(step: WiringStep) -> None:
            if step.technical:
                self._append_diagnostics(self._redact(step.technical, install))
            wiring_rows[step.index] = replace(step, technical=None)
            self._emit(
                DeploySnapshot(
                    run_id=run_id,
                    phase="wiring",
                    apps=tuple(progresses),
                    headline=PHASE_HEADLINE_WIRING,
                    detail=None,
                    failure=None,
                    started_at=started_at,
                    finished_at=None,
                    wiring=tuple(wiring_rows[index] for index in sorted(wiring_rows)),
                )
            )

        try:
            await self._wiring.run(install, collect_wiring_step)
        except Exception:
            # A wiring problem is not a deploy failure - the apps are
            # already up and done, so this never turns a successful deploy
            # into one that looks failed.
            logger.exception("wiring runner raised; continuing to finale anyway")

        wiring_steps = tuple(wiring_rows[index] for index in sorted(wiring_rows))
        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="finale",
                apps=tuple(progresses),
                headline=PHASE_HEADLINE_FINALE,
                detail=_wiring_finale_detail(wiring_steps),
                failure=None,
                started_at=started_at,
                finished_at=_now_iso(),
                wiring=wiring_steps,
            )
        )
        if self._hardlinks is not None:
            # Fired, never awaited: a hung drive must never pin this task
            # as "busy" - the check runs and saves on its own, and the next
            # poll or Diagnostics visit sees its result.
            self._hardlinks.request_check()

        if self._recyclarr is not None and sync_wanted_after(install.app_ids, None):
            # Fired, never awaited, the same fire-and-forget shape as the
            # drive check just above - a slow sync must never pin this
            # deploy as still running.
            self._recyclarr.request_sync()

    async def _find_name_clash(
        self, install: InstallState, root: PurePosixPath, catalog_apps: tuple[CatalogApp, ...]
    ) -> Failure | None:
        marker = read_marker(self._settings, root)
        owned_ids = frozenset(marker.app_ids) if marker is not None else frozenset()
        for app in catalog_apps:
            if not app.managed:
                continue  # never Marrquee's to create, so never Marrquee's to clash with
            if app.id in owned_ids:
                continue  # a container we created on an earlier attempt at this same root
            existing = await self._engine.inspect(app.id)
            if existing.exists:
                headline, what_to_do = _split_failure_text(refusal_name_clash(app.name))
                return Failure(
                    code="name_clash",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"a container named {app.id!r} already exists and isn't ours",
                )
        return None

    def _full_deploy_reporter(
        self,
        progresses: list[AppProgress],
        index: int,
        app: CatalogApp,
        run_id: str,
        started_at: str,
    ) -> Callable[[AppState, str, str | None], Awaitable[None]]:
        """The full deploy's own `report` callback for `_bring_up_app`.

        Reproduces exactly the snapshot the inline code used to publish at
        each of its three call sites - the existing `test_deploy` suite is
        what proves this refactor changed no observable behaviour.
        """
        chip_for_state = {"starting": STATUS_CHIP_STARTING, "done": STATUS_CHIP_DONE}
        headline_for_state = {
            "starting": app_headline_starting(app.name),
            "done": app_headline_done(app.name),
        }

        async def report(state: AppState, line: str, note: str | None) -> None:
            progresses[index] = replace(
                progresses[index], state=state, chip=chip_for_state[state], line=line, note=note
            )
            await self._publish(
                _running_snapshot(run_id, started_at, progresses, headline_for_state[state])
            )

        return report

    async def _bring_up_app(
        self,
        app: CatalogApp,
        install: InstallState,
        root: PurePosixPath,
        compose_path: Path,
        network: str,
        self_id: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
        *,
        recreate: bool = False,
    ) -> Failure | None:
        if not app.managed:
            # The owner's own Plex: never Marrquee's to start, so never
            # Marrquee's key, image, compose or network to touch either -
            # this has to be the very first thing checked, before even the
            # ordinary (unused) API key every app still gets minted.
            return await self._bring_up_existing_plex(app, report)

        api_key = install.api_keys.get(app.id)
        if api_key is None:
            headline, what_to_do = _split_failure_text(
                refusal_path_missing(install.storage_root or "")
            )
            return Failure(
                code="storage_refused",
                headline=headline,
                what_to_do=what_to_do,
                technical=f"no API key was recorded for {app.id!r}",
            )

        if app.kind == "media_server":
            if app.id == JELLYFIN_APP_ID:
                return await self._bring_up_jellyfin(
                    app, install, root, compose_path, self_id, report, recreate=recreate
                )
            return await self._bring_up_plex(
                app, install, root, compose_path, self_id, report, recreate=recreate
            )

        if app.kind == "requests":
            # Seerr has no `/api/v1/system/status` for the generic readiness
            # loop below to probe, so it must never fall through to it -
            # its own bring-up proves readiness through its own first-run
            # setup instead.
            return await self._bring_up_seerr(
                app, install, compose_path, network, self_id, report, recreate=recreate
            )

        if app.kind == "vpn":
            secrets_failure = self._write_vpn_secrets(app, install, root)
            if secrets_failure is not None:
                return secrets_failure

        if app.kind == "downloader":
            conf_failure = self._write_qbit_conf_file(app, api_key, root, install)
            if conf_failure is not None:
                return conf_failure

        if app.kind == "sync":
            conf_failure = self._write_recyclarr_conf(install, root)
            if conf_failure is not None:
                return conf_failure

        fetching_image = not await self._engine.image_present(app.image)
        line = app_line_getting(app.name) if fetching_image else app_line_starting(app.name)
        note: str | None = None
        await report("starting", line, note)

        result = await self._engine.compose_up(
            self._settings.stack_project, compose_path, app.id, recreate=recreate
        )
        if not result.ok:
            if app.kind == "vpn" and looks_like_missing_tun(result.output):
                headline, what_to_do = _split_failure_text(FAILURE_VPN_NO_TUN)
                return Failure(
                    code="vpn_no_tun",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=result.output,
                )
            riders = riders_of(app.id, install.app_ids) if app.kind == "vpn" else ()
            return _compose_failure(app, fetching_image, result, riders)

        # Compose creates the stack's network as a side effect of its own
        # first successful `up` - on a fresh host, nothing exists before
        # that, so this can never run any earlier. Idempotent (an
        # already-connected container counts as success) and repeated for
        # every app rather than once, so a Marrquee container recreated
        # mid-deploy (a restart) rejoins the network here instead of
        # silently staying off it.
        connect_result = await self._engine.connect_network(network, self_id)
        if not connect_result.ok:
            return _docker_unreachable_failure(
                connect_result.detail
                or f"could not join the {network!r} network (self_id={self_id!r})"
            )

        if app.kind == "vpn":
            # Replaces the arr readiness loop entirely: proof here is
            # Docker's own health check AND Gluetun's own status AND a
            # reported address - never an outside IP service, since Gluetun
            # only ever answers from inside its own firewall.
            return await self._await_tunnel(app, api_key, report)

        start = self._clock()
        reassured = False
        while True:
            elapsed = self._clock() - start
            if elapsed >= self.NEVER_READY_AFTER_SECONDS:
                logs = await self._engine.logs(app.id, tail=50)
                headline, what_to_do = _split_failure_text(failure_never_became_ready(app.name))
                return Failure(
                    code="never_became_ready",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=logs,
                )

            container = await self._engine.inspect(app.id)
            if container.state == "running":
                if await self._app_ready(app, api_key, install.app_ids):
                    await report("done", app_line_done(app.name), None)
                    return None
                candidate_line = app_line_warming_up(app.name)
            else:
                candidate_line = line

            candidate_note = note
            if not reassured and elapsed >= self.REASSURANCE_AFTER_SECONDS:
                reassured = True
                candidate_note = app_note_slow_start(app.name)

            if candidate_line != line or candidate_note != note:
                line, note = candidate_line, candidate_note
                await report("starting", line, note)

            await self._sleep(self.POLL_INTERVAL_SECONDS)

    # --- The owner's own Plex: no container, only a health check -----------

    async def _bring_up_existing_plex(
        self, app: CatalogApp, report: Callable[[AppState, str, str | None], Awaitable[None]]
    ) -> Failure | None:
        """The unmanaged "bring-up": the saved address already proved it
        can reach this Plex once, so all that's left is proving it still
        answers as the same Plex - no image check, no `compose_up`, no
        network join and no readiness loop, because none of those were
        ever ours to run for a container we never created.
        """
        record = load_existing_plex(self._settings.config_dir)
        if record is None:
            return self._existing_plex_failure(technical="no saved connection to the owner's Plex")
        identity = await self._plex_server.identity(record.base_url)
        if identity is not None and identity.machine_id == record.machine_id:
            await report("done", app_line_done(app.name), None)
            return None
        seen = identity.machine_id if identity is not None else "nothing"
        return self._existing_plex_failure(technical=f"{record.base_url}: identity answered {seen}")

    def _existing_plex_failure(self, *, technical: str) -> Failure:
        headline, what_to_do = _split_failure_text(FAILURE_EXISTING_PLEX_UNREACHABLE)
        return Failure(
            code="existing_plex_unreachable",
            headline=headline,
            what_to_do=what_to_do,
            technical=technical,
        )

    async def _app_ready(self, app: CatalogApp, api_key: str, present: Iterable[str]) -> bool:
        """Whether `app` has finished booting and will accept its key.

        qBittorrent has no `system/status` (it isn't an arr app) - proof
        is a live, keyed call to its own `app/version` instead, through
        the same `QbitClient` its settings step and its login use.
        `present` is the install's own app ids, so this reaches qBittorrent
        at whichever host it actually answers on - `gluetun` behind the
        VPN, `qbittorrent` on its own network.

        A `kind == "sync"` app has no web page to probe at all - Docker
        reporting the container `running` (already checked by the caller's
        own loop) is the whole of "ready" for it.
        """
        if app.kind == "sync":
            return True
        if app.kind == "downloader":
            response = await self._qbit.request(
                "GET", app_base_url(app, present), f"{app.api_base}/app/version", api_key
            )
            return response.ok
        return await self._probe.check(app.id, require_port(app), app.api_base, api_key)

    def _write_qbit_conf_file(
        self, app: CatalogApp, api_key: str, root: PurePosixPath, install: InstallState
    ) -> Failure | None:
        """Write qBittorrent's settings file - its key, never a temporary
        password - before Docker is ever asked to start it.

        An existing file (the owner's own, or an earlier Marrquee run's) is
        left untouched; only a genuine write problem (a full disk, a
        symlink planted where the folder should be) is a real failure here.
        """
        try:
            write_qbit_conf(self._settings, root, api_key, install.puid, install.pgid)
        except (OSError, PathEscapesRoot) as error:
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            return Failure(
                code="compose_failed",
                headline=headline,
                what_to_do=what_to_do,
                technical=str(error),
            )
        return None

    def _write_recyclarr_conf(self, install: InstallState, root: PurePosixPath) -> Failure | None:
        """Rewrite Recyclarr's own config from the install and saved
        quality answers, right before Docker is ever asked to start it -
        the file must already hold this install's exact choices the very
        first time Recyclarr's own cron ever runs, the same idea
        `_write_qbit_conf_file` follows for qBittorrent's own settings.
        """
        answers = load_answers(self._settings.config_dir)
        try:
            write_recyclarr_config(self._settings, install, answers)
        except (OSError, ValueError, PathEscapesRoot) as error:
            app = get_app(RECYCLARR_APP_ID)
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            return Failure(
                code="compose_failed",
                headline=headline,
                what_to_do=what_to_do,
                technical=self._redact(str(error), install),
            )
        return None

    # --- Proving the tunnel: Gluetun's own branch of `_bring_up_app` -------

    def _write_vpn_secrets(
        self, app: CatalogApp, install: InstallState, root: PurePosixPath
    ) -> Failure | None:
        """Rewrite Gluetun's root-only secrets folder from the saved
        answers, right before Docker is ever asked to start it.

        Goes through `gluetun_config_for` - the same helper `_vpn_service_plan`
        builds compose from - rather than calling `build_gluetun_config`
        directly: `write_vpn_secrets` deletes every file not handed to it on
        every call, so building this any other way would silently wipe
        qBittorrent's key and its port-sync script the moment Gluetun next
        reconnects. `gluetun_config_for` itself never raises here: the
        `_vpn_settings_failure` gate already ran, in this same caller,
        before `build_stack_plan` was ever called - by the time this runs,
        the saved answers have already passed `check_vpn_answers` once, and
        `install.api_keys` already holds this app's own key (checked by
        this same caller just before).
        """
        answers = load_answers(self._settings.config_dir)
        config = gluetun_config_for(app, install, answers)
        try:
            write_vpn_secrets(self._settings, root, config.secret_files)
        except OSError as error:
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            return Failure(
                code="compose_failed",
                headline=headline,
                what_to_do=what_to_do,
                technical=str(error),
            )
        return None

    def _vpn_company_label(self, app: CatalogApp) -> str:
        """The plain company name for a failure sentence like "ProtonVPN
        refused...". Falls back to the app's own name (`"VPN"`) rather than
        raising - the `_vpn_settings_failure` gate already keeps an
        unrecognised provider out of this code path in practice.
        """
        answers = load_answers(self._settings.config_dir).get(app.id, {})
        provider = find_provider(answers.get("provider", ""))
        return provider.label if provider is not None else app.name

    async def _await_tunnel(
        self,
        app: CatalogApp,
        api_key: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
    ) -> Failure | None:
        """The tunnel loop: replaces the arr readiness loop entirely for a
        `kind == "vpn"` app, in the same waiting/reassurance shape.

        Proof is Docker's own health check AND Gluetun's own status AND a
        reported address - a healthy, running tunnel that never reports a
        place within `TUNNEL_PLACE_GRACE_SECONDS` still counts as proven
        (the plain "protected" line, never an invented city). Nothing here
        is persisted beyond the final `report("done", ...)` line: a stored
        place would lie the moment Gluetun reconnects to a different
        server.
        """
        company = self._vpn_company_label(app)
        start = self._clock()
        reassured = False
        healthy_since: float | None = None
        note: str | None = None
        await report("starting", VPN_LINE_CONNECTING, note)

        while True:
            elapsed = self._clock() - start
            if elapsed >= self.TUNNEL_NEVER_UP_AFTER_SECONDS:
                logs = await self._engine.logs(app.id, tail=80)
                minutes = round(self.TUNNEL_NEVER_UP_AFTER_SECONDS / 60)
                return _vpn_verdict_failure(
                    "vpn_not_connected", failure_vpn_not_connected(minutes), logs
                )

            container = await self._engine.inspect(app.id)
            logs = await self._engine.logs(app.id, tail=80)
            verdict = classify_tunnel(container, logs)

            if verdict == "refused":
                return _vpn_verdict_failure("vpn_refused", failure_vpn_refused(company), logs)
            if verdict == "settings_refused":
                return _vpn_verdict_failure(
                    "vpn_settings_refused", failure_vpn_settings_refused(company), logs
                )
            if verdict == "gone":
                headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
                return Failure(
                    code="compose_failed", headline=headline, what_to_do=what_to_do, technical=logs
                )

            if verdict == "healthy":
                status = await self._vpn.vpn_status(api_key)
                if status == "running":
                    place = await self._vpn.public_ip(api_key)
                    if place is not None:
                        await report("done", tunnel_place_line(place), None)
                        return None
                    healthy_since = elapsed if healthy_since is None else healthy_since
                    if elapsed - healthy_since >= self.TUNNEL_PLACE_GRACE_SECONDS:
                        await report("done", tunnel_place_line(None), None)
                        return None
                else:
                    healthy_since = None
            else:
                healthy_since = None

            if not reassured and elapsed >= self.REASSURANCE_AFTER_SECONDS:
                reassured = True
                note = VPN_NOTE_SLOW
                await report("starting", VPN_LINE_CONNECTING, note)

            await self._sleep(self.POLL_INTERVAL_SECONDS)

    # --- Plex's own bring-up: a claim, not a key, proves it's ours ---------

    async def _bring_up_plex(
        self,
        app: CatalogApp,
        install: InstallState,
        root: PurePosixPath,
        compose_path: Path,
        self_id: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
        *,
        recreate: bool = False,
    ) -> Failure | None:
        """Plex's own branch of `_bring_up_app`: no shared Docker network to
        join (host networking has none), and "ready" means plex.tv's own
        claim took, not merely that a probe answered.

        Kept apart from the arr/vpn/downloader/sync branches above so those
        stay readable. The `finally` is what keeps a claim code - good for a
        few minutes at most - from ever surviving past the one bring-up
        that used it, win or lose.
        """
        try:
            return await self._run_plex_bring_up(
                app, install, root, compose_path, self_id, report, recreate=recreate
            )
        finally:
            clear_plex_claim(self._settings, root)

    async def _run_plex_bring_up(
        self,
        app: CatalogApp,
        install: InstallState,
        root: PurePosixPath,
        compose_path: Path,
        self_id: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
        *,
        recreate: bool,
    ) -> Failure | None:
        account = load_plex_account(self._settings.config_dir)
        if account is None or account.token is None:
            return self._plex_failure(
                "plex_sign_in_needed",
                FAILURE_PLEX_SIGN_IN_NEEDED,
                technical="no Plex sign-in is saved",
            )

        address = await plex_host_address(self._engine, self_id)
        if address is None:
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            return Failure(
                code="compose_failed",
                headline=headline,
                what_to_do=what_to_do,
                technical="could not work out this machine's address from Marrquee's own networks",
            )
        base_url = plex_base_url(address)

        existing = await self._engine.inspect(app.id)
        if not existing.exists:
            pre_flight = await self._plex_server.identity(base_url)
            if pre_flight is not None:
                return self._plex_failure(
                    "plex_port_taken",
                    FAILURE_PLEX_PORT_TAKEN,
                    technical=f"something already answers on port {require_port(app)}",
                )
            claim_failure = await self._claim_plex(app, root, account.client_id, account.token)
            if claim_failure is not None:
                return claim_failure

        fetching_image = not await self._engine.image_present(app.image)
        line = app_line_getting(app.name) if fetching_image else app_line_starting(app.name)
        await report("starting", line, None)

        result = await self._engine.compose_up(
            self._settings.stack_project, compose_path, app.id, recreate=recreate
        )
        if not result.ok:
            return _compose_failure(app, fetching_image, result)

        return await self._await_plex_claimed(
            app, root, compose_path, account.client_id, account.token, base_url, report
        )

    def _plex_failure(self, code: FailureCode, message: str, *, technical: str) -> Failure:
        headline, what_to_do = _split_failure_text(message)
        return Failure(code=code, headline=headline, what_to_do=what_to_do, technical=technical)

    async def _claim_plex(
        self, app: CatalogApp, root: PurePosixPath, client_id: str, token: str
    ) -> Failure | None:
        claim = await self._plex_tv.claim_token(client_id, token)
        if claim is None:
            return self._plex_failure(
                "plex_sign_in_needed",
                FAILURE_PLEX_SIGN_IN_NEEDED,
                technical="no claim code was issued for Plex",
            )
        try:
            write_plex_claim(self._settings, root, claim)
        except (OSError, PathEscapesRoot) as error:
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            return Failure(
                code="compose_failed",
                headline=headline,
                what_to_do=what_to_do,
                technical=str(error),
            )
        return None

    async def _await_plex_claimed(
        self,
        app: CatalogApp,
        root: PurePosixPath,
        compose_path: Path,
        client_id: str,
        token: str,
        base_url: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
    ) -> Failure | None:
        """Wait for Docker to report the container running AND plex.tv's own
        claim to have taken - modelled on `_await_tunnel`'s own loop.

        `init-plex-claim` briefly starts PMS before it ever claims, so one
        unclaimed `/identity` reply proves nothing by itself (the design
        correction this loop exists to honour): it only means "claim
        failed" once the container's own logs say so, or once unclaimed
        answers persist continuously for `PLEX_UNCLAIMED_GRACE_SECONDS`.
        The first such verdict gets exactly one fresh code and one
        recreate (`retried`, local to this one bring-up); a second verdict
        is `plex_not_claimed`.
        """
        start = self._clock()
        reassured = False
        retried = False
        unclaimed_since: float | None = None
        line = app_line_starting(app.name)
        note: str | None = None
        await report("starting", line, note)

        while True:
            elapsed = self._clock() - start
            if elapsed >= self.NEVER_READY_AFTER_SECONDS:
                logs = await self._engine.logs(app.id, tail=50)
                headline, what_to_do = _split_failure_text(failure_never_became_ready(app.name))
                return Failure(
                    code="never_became_ready",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=logs,
                )

            container = await self._engine.inspect(app.id)
            identity = (
                await self._plex_server.identity(base_url) if container.state == "running" else None
            )

            if identity is not None:
                if identity.claimed:
                    await report("done", app_line_done(app.name), None)
                    return None

                if unclaimed_since is None:
                    unclaimed_since = elapsed
                claim_failed_log = "Unable to claim Plex server" in await self._engine.logs(
                    app.id, tail=200
                )
                grace_expired = (elapsed - unclaimed_since) >= self.PLEX_UNCLAIMED_GRACE_SECONDS
                if claim_failed_log or grace_expired:
                    if retried:
                        logs = await self._engine.logs(app.id, tail=50)
                        return self._plex_failure(
                            "plex_not_claimed", FAILURE_PLEX_NOT_CLAIMED, technical=logs
                        )
                    retry_failure = await self._retry_plex_claim(
                        app, root, compose_path, client_id, token
                    )
                    if retry_failure is not None:
                        return retry_failure
                    retried = True
                    unclaimed_since = None
                    await self._sleep(self.POLL_INTERVAL_SECONDS)
                    continue
            else:
                unclaimed_since = None

            candidate_note = note
            if not reassured and elapsed >= self.REASSURANCE_AFTER_SECONDS:
                reassured = True
                candidate_note = app_note_slow_start(app.name)
            if candidate_note != note:
                note = candidate_note
                await report("starting", line, note)

            await self._sleep(self.POLL_INTERVAL_SECONDS)

    async def _retry_plex_claim(
        self, app: CatalogApp, root: PurePosixPath, compose_path: Path, client_id: str, token: str
    ) -> Failure | None:
        """One recovery attempt: a fresh claim code, written fresh, behind a
        removed-and-recreated container - `init-plex-claim` only reads the
        secret again on a genuinely new start, never on an already-running
        one.
        """
        claim_failure = await self._claim_plex(app, root, client_id, token)
        if claim_failure is not None:
            return claim_failure
        remove_result = await self._engine.remove_container(app.id)
        if not remove_result.ok:
            return _docker_unreachable_failure(
                remove_result.detail or f"could not remove {app.id!r}"
            )
        result = await self._engine.compose_up(self._settings.stack_project, compose_path, app.id)
        if not result.ok:
            return _compose_failure(app, False, result)
        return None

    # --- Jellyfin's own bring-up: Marrquee's first-time setup proves it ----

    async def _bring_up_jellyfin(
        self,
        app: CatalogApp,
        install: InstallState,
        root: PurePosixPath,
        compose_path: Path,
        self_id: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
        *,
        recreate: bool = False,
    ) -> Failure | None:
        """Jellyfin's own branch of `_bring_up_app`: host networking (the
        same reachable address Plex uses), and "ready" means Marrquee's own
        first-time setup made the one login its admin - not merely that a
        probe answered.

        Simpler than Plex's own branch: there is no claim code and no
        grace-period retry, so the loop below never touches
        `connect_network` or the arr `_app_ready` probe either.
        """
        login = load_login(self._settings.config_dir).login
        if login is None:
            return self._jellyfin_failure(
                "jellyfin_setup_refused",
                FAILURE_JELLYFIN_SETUP_REFUSED,
                technical="no login is saved",
            )

        address = await plex_host_address(self._engine, self_id)
        if address is None:
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            return Failure(
                code="compose_failed",
                headline=headline,
                what_to_do=what_to_do,
                technical="could not work out this machine's address from Marrquee's own networks",
            )
        base_url = jellyfin_base_url(address)

        existing = await self._engine.inspect(app.id)
        if not existing.exists:
            pre_flight = await self._jellyfin.request(
                "GET", base_url, "/System/Info/Public", token=None
            )
            if pre_flight.status != 0:
                return self._jellyfin_failure(
                    "jellyfin_port_taken",
                    FAILURE_JELLYFIN_PORT_TAKEN,
                    technical=f"something already answers on port {require_port(app)}",
                )

        fetching_image = not await self._engine.image_present(app.image)
        line = app_line_getting(app.name) if fetching_image else app_line_starting(app.name)
        await report("starting", line, None)

        result = await self._engine.compose_up(
            self._settings.stack_project, compose_path, app.id, recreate=recreate
        )
        if not result.ok:
            return _compose_failure(app, fetching_image, result)

        return await self._await_jellyfin_admin(app, login, base_url, report)

    def _jellyfin_failure(self, code: FailureCode, message: str, *, technical: str) -> Failure:
        headline, what_to_do = _split_failure_text(message)
        return Failure(code=code, headline=headline, what_to_do=what_to_do, technical=technical)

    async def _await_jellyfin_admin(
        self,
        app: CatalogApp,
        login: SavedLogin,
        base_url: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
    ) -> Failure | None:
        """Wait for Docker to report the container running AND Marrquee's
        own first-time setup to have made `login` the admin - modelled on
        `_await_plex_claimed`'s own loop, minus the claim code and its
        grace-period retry (Jellyfin's setup has neither).
        """
        start = self._clock()
        reassured = False
        line = app_line_starting(app.name)
        note: str | None = None
        await report("starting", line, note)

        while True:
            elapsed = self._clock() - start
            if elapsed >= self.NEVER_READY_AFTER_SECONDS:
                logs = await self._engine.logs(app.id, tail=50)
                headline, what_to_do = _split_failure_text(failure_never_became_ready(app.name))
                return Failure(
                    code="never_became_ready",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=logs,
                )

            container = await self._engine.inspect(app.id)
            candidate_line = line
            if container.state == "running":
                setup = await ensure_jellyfin_admin(
                    self._jellyfin,
                    base_url,
                    login,
                    self._settings.config_dir,
                    server_name=JELLYFIN_SERVER_NAME,
                )
                if setup.state == "done":
                    await report("done", app_line_done(app.name), None)
                    return None
                if setup.state == "not_ours":
                    return self._jellyfin_failure(
                        "jellyfin_not_ours",
                        FAILURE_JELLYFIN_NOT_OURS,
                        technical=setup.technical or "jellyfin: admin is not ours",
                    )
                if setup.state == "refused":
                    return self._jellyfin_failure(
                        "jellyfin_setup_refused",
                        FAILURE_JELLYFIN_SETUP_REFUSED,
                        technical=setup.technical or "jellyfin: setup refused",
                    )
                # "waiting": Jellyfin is running but hasn't answered a real
                # request yet - the same "still booting" story the warming-up
                # line already tells for every other app.
                candidate_line = app_line_warming_up(app.name)

            candidate_note = note
            if not reassured and elapsed >= self.REASSURANCE_AFTER_SECONDS:
                reassured = True
                candidate_note = app_note_slow_start(app.name)

            if candidate_line != line or candidate_note != note:
                line, note = candidate_line, candidate_note
                await report("starting", line, note)

            await self._sleep(self.POLL_INTERVAL_SECONDS)

    # --- Seerr: no port pre-flight, no generic readiness probe --------------

    async def _bring_up_seerr(
        self,
        app: CatalogApp,
        install: InstallState,
        compose_path: Path,
        network: str,
        self_id: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
        *,
        recreate: bool = False,
    ) -> Failure | None:
        """Seerr's own branch of `_bring_up_app`: a sign-in must already be
        on disk before anything starts, and "ready" means Marrquee's own
        first-time setup (`ensure_seerr_setup`) says so - never merely that
        the container answers, since Seerr has no `system/status` route for
        the generic loop to probe.
        """
        config_dir = self._settings.config_dir
        kind = seerr_sign_in_kind(install.app_ids, config_dir)
        if kind is None:
            return self._seerr_failure(
                "seerr_setup_refused",
                FAILURE_SEERR_SETUP_REFUSED,
                technical="no sign-in saved for seerr",
            )
        if kind == "jellyfin" and JELLYFIN_APP_ID in pending_app_ids(
            load_login(config_dir), install.app_ids
        ):
            return self._seerr_failure(
                "seerr_setup_refused",
                FAILURE_SEERR_SETUP_REFUSED,
                technical="the one login has not reached jellyfin yet",
            )

        fetching_image = not await self._engine.image_present(app.image)
        line = app_line_getting(app.name) if fetching_image else app_line_starting(app.name)
        await report("starting", line, None)

        result = await self._engine.compose_up(
            self._settings.stack_project, compose_path, app.id, recreate=recreate
        )
        if not result.ok:
            return _compose_failure(app, fetching_image, result)

        connect_result = await self._engine.connect_network(network, self_id)
        if not connect_result.ok:
            return _docker_unreachable_failure(
                connect_result.detail
                or f"could not join the {network!r} network (self_id={self_id!r})"
            )

        return await self._await_seerr_setup(app, install, kind, report)

    async def _await_seerr_setup(
        self,
        app: CatalogApp,
        install: InstallState,
        kind: Literal["plex", "jellyfin"],
        report: Callable[[AppState, str, str | None], Awaitable[None]],
    ) -> Failure | None:
        config_dir = self._settings.config_dir
        start = self._clock()
        reassured = False
        line = app_line_starting(app.name)
        note: str | None = None
        last_technical: str | None = None

        while True:
            elapsed = self._clock() - start
            if elapsed >= self.NEVER_READY_AFTER_SECONDS:
                logs = await self._engine.logs(app.id, tail=50)
                technical = logs if last_technical is None else f"{logs}\n{last_technical}"
                headline, what_to_do = _split_failure_text(failure_never_became_ready(app.name))
                return Failure(
                    code="never_became_ready",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=technical,
                )

            container = await self._engine.inspect(app.id)
            candidate_line = line
            if container.state == "running":
                gateway = await self._engine.host_gateway(app.id)
                sign_in = seerr_sign_in(kind, config_dir, jellyfin_host=gateway)
                if sign_in is None:
                    # The gateway (or, for Jellyfin, the saved login) isn't
                    # there yet - the same "still booting" story as a
                    # container that hasn't answered a real request yet.
                    candidate_line = app_line_warming_up(app.name)
                else:
                    setup = await ensure_seerr_setup(
                        self._seerr, seerr_base_url(), install.api_keys[app.id], sign_in
                    )
                    last_technical = setup.technical
                    if setup.state == "done":
                        await report("done", app_line_done(app.name), None)
                        return None
                    if setup.state == "not_ours":
                        return self._seerr_failure(
                            "seerr_not_ours",
                            FAILURE_SEERR_NOT_OURS,
                            technical=setup.technical or "seerr: settings are not ours",
                        )
                    if setup.state == "refused":
                        return self._seerr_failure(
                            "seerr_setup_refused",
                            FAILURE_SEERR_SETUP_REFUSED,
                            technical=setup.technical or "seerr: setup refused",
                        )
                    # "waiting": Seerr is running but hasn't answered a real
                    # request yet.
                    candidate_line = app_line_warming_up(app.name)

            candidate_note = note
            if not reassured and elapsed >= self.REASSURANCE_AFTER_SECONDS:
                reassured = True
                candidate_note = app_note_slow_start(app.name)

            if candidate_line != line or candidate_note != note:
                line, note = candidate_line, candidate_note
                await report("starting", line, note)

            await self._sleep(self.POLL_INTERVAL_SECONDS)

    def _seerr_failure(self, code: FailureCode, message: str, *, technical: str) -> Failure:
        headline, what_to_do = _split_failure_text(message)
        return Failure(code=code, headline=headline, what_to_do=what_to_do, technical=technical)

    # --- Running an add or a retry -----------------------------------------

    def _diagnostics_recorder(self) -> Callable[[str], None]:
        """A `record(text)` closure for one add/reconnect run: the FIRST
        call replaces the diagnostics file, every later call appends -
        "Last problem" is replaced only by a newer problem, and an add with
        nothing to say about it never touches an older run's evidence.
        """
        wrote = False

        def record(text: str) -> None:
            nonlocal wrote
            if not wrote:
                self._diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
                self._diagnostics_file.write_text(text if text.endswith("\n") else f"{text}\n")
                wrote = True
            else:
                self._append_diagnostics(text)

        return record

    async def _run_add(self, app: CatalogApp, install: InstallState) -> None:
        """Run a whole add (or a retry of one) to completion.

        `install` is the ORIGINAL, on-disk state - not yet grown - the same
        object whether this is a fresh `add_app`, a `retry_add`, or a
        resumed one; `_run_add_steps` is the one place that decides whether
        `app` already belongs to it.
        """
        record_diagnostics = self._diagnostics_recorder()
        try:
            await self._run_add_steps(app, install, record_diagnostics=record_diagnostics)
        except Exception as error:  # the background task must never die silently
            logger.exception("add-app run crashed unexpectedly")
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            await self._fail_add(
                app,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
                record_diagnostics,
            )

    async def _run_add_steps(
        self, app: CatalogApp, install: InstallState, *, record_diagnostics: Callable[[str], None]
    ) -> None:
        # `current.with_apps` (set once, by `add_app`/`retry_add`, before
        # this ever runs) is the single source of truth for which
        # companion(s) belong to this add - a resumed add reads the exact
        # same set rather than re-deriving it against an install that may
        # have grown in the meantime.
        current = self._current_adding()
        companions = current.with_apps if current is not None else ()
        movers = apps_in_order(current.moves) if current is not None else ()
        via_id = app.network_via
        via_already_installed = via_id is not None and via_id in set(install.app_ids)

        # Idempotent: an already-grown state (a resumed add) just re-keeps
        # every existing id and key - this is what lets every caller
        # (add_app, retry_add, a resume) pass the ORIGINAL, on-disk install
        # and let this one call decide whether it's already grown.
        new_ids = (*companions, app.id)
        grown = install
        for new_id in new_ids:
            grown = with_app_added(grown, new_id)
        save_state(self._settings.config_dir, grown)

        docker_status = await self._engine.status()
        if not docker_status.connected:
            await self._fail_add(
                app,
                grown,
                _docker_unreachable_failure(docker_status.detail or "Docker did not answer"),
                record_diagnostics,
            )
            return

        if grown.storage_root is None:
            headline, what_to_do = _split_failure_text(
                refusal_path_missing("(no storage folder chosen)")
            )
            await self._fail_add(
                app,
                grown,
                Failure(
                    code="storage_refused",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical="no storage_root was ever saved",
                ),
                record_diagnostics,
            )
            return

        storage_check = check_storage_root(self._settings, grown.storage_root)
        if not storage_check.ok:
            await self._fail_add(
                app,
                grown,
                _storage_failure(storage_check, grown.storage_root),
                record_diagnostics,
            )
            return

        root = PurePosixPath(grown.storage_root)
        freshness = check_fresh_start(self._settings, root, grown.app_ids)
        if not freshness.ok:
            await self._fail_add(
                app, grown, _freshness_failure(freshness, grown.storage_root), record_diagnostics
            )
            return

        clash = await self._find_name_clash(grown, root, apps_in_order(new_ids))
        if clash is not None:
            await self._fail_add(app, grown, clash, record_diagnostics)
            return

        build_folders(self._settings, root, grown.app_ids, grown.puid, grown.pgid)
        write_marker(self._settings, root, grown.app_ids, grown.puid, grown.pgid)
        answers = load_answers(self._settings.config_dir)
        vpn_failure = _vpn_settings_failure(grown.app_ids, answers)
        if vpn_failure is not None:
            await self._fail_add(app, grown, vpn_failure, record_diagnostics)
            return
        plan = self._stack_plan(grown)
        compose_path = write_compose(self._settings, plan)

        self_id = await self._engine.self_container_id()
        if self_id is None:
            await self._fail_add(
                app,
                grown,
                _docker_unreachable_failure("could not identify Marrquee's own container"),
                record_diagnostics,
            )
            return

        current = self._current_adding()
        if current is not None and app.managed:
            self._emit(self._replace_finale(adding=replace(current, compose_ran=True)))

        report = self._add_reporter()

        if movers:
            take_down_failure = await self._take_down(movers)
            if take_down_failure is not None:
                await self._fail_add(app, grown, take_down_failure, record_diagnostics)
                return

        if via_already_installed and via_id is not None:
            # The via app already exists, running its OWN key/settings -
            # bringing the rider up beside it as-is would leave the
            # rider's port unpublished and its own key never given to it.
            # Remove it and bring it up again through the SAME
            # `_bring_up_app` every other bring-up uses (never
            # `recreate=True`, which leaves an unchanged compose service,
            # and its shared network namespace, alone).
            via_app = get_app(via_id)
            remove_result = await self._engine.remove_container(via_app.id)
            if not remove_result.ok:
                await self._fail_add(
                    app,
                    grown,
                    _docker_unreachable_failure(
                        remove_result.detail or f"could not remove {via_app.id!r}"
                    ),
                    record_diagnostics,
                )
                return
            failure = await self._bring_up_app(
                via_app, grown, root, compose_path, plan.network, self_id, report
            )
            if failure is not None:
                await self._fail_add(app, grown, failure, record_diagnostics)
                return
        else:
            for companion_id in companions:
                companion_app = get_app(companion_id)
                failure = await self._bring_up_app(
                    companion_app, grown, root, compose_path, plan.network, self_id, report
                )
                if failure is not None:
                    await self._fail_add(app, grown, failure, record_diagnostics)
                    return

        failure = await self._bring_up_app(
            app, grown, root, compose_path, plan.network, self_id, report
        )
        if failure is not None:
            await self._fail_add(app, grown, failure, record_diagnostics)
            return

        # Only reached once the VPN app's own proof (above) has succeeded -
        # a refused or never-up tunnel returns before this point, so a
        # mover is never recreated behind a VPN that was never proven.
        for mover in movers:
            mover_failure = await self._bring_up_app(
                mover, grown, root, compose_path, plan.network, self_id, report
            )
            if mover_failure is not None:
                await self._fail_add(app, grown, mover_failure, record_diagnostics)
                return
            if mover.id in pending_app_ids(load_login(self._settings.config_dir), grown.app_ids):
                await self._put_login(mover, grown, record_diagnostics=record_diagnostics)

        for new_id in new_ids:
            await self._put_login(get_app(new_id), grown, record_diagnostics=record_diagnostics)

        current = self._current_adding()
        if current is not None:
            self._emit(
                self._replace_finale(
                    adding=replace(current, state="wiring", line=hub_line_connecting(app.name))
                )
            )

        mover_ids = tuple(mover.id for mover in movers)
        await self._run_wiring_for_add(
            app, grown, record_diagnostics, also_ids=companions, wire_ids=mover_ids or (app.id,)
        )

        if app.id == EXISTING_PLEX_APP_ID:
            self._drop_replaced_link()

        if movers:
            # A successful move is the one thing that turns the badge off -
            # a failed one (caught above, before this line) keeps it lit.
            clear_without_vpn(self._settings.config_dir)

        if self._hardlinks is not None:
            # The one place every successful add/retry/resumed add ends -
            # reconnect, Change VPN and restore each end in
            # `_run_wiring_for_add` directly and never reach here, so
            # moving an existing pair around never asks for a needless check.
            self._hardlinks.request_check()

        if self._recyclarr is not None and sync_wanted_after(grown.app_ids, app.id):
            # Same reasoning as the drive check just above: reconnect,
            # Change VPN and restore never reach this line, so none of them
            # ever asks for a needless sync.
            self._recyclarr.request_sync()

    def _add_reporter(self) -> Callable[[AppState, str, str | None], Awaitable[None]]:
        """The add path's own `report` callback for `_bring_up_app`.

        Only `line`/`note` change here - `adding.state` stays `"starting"`
        throughout the whole call (the explicit transition to `"wiring"`
        happens once, right after `_bring_up_app` returns), so this never
        needs to know which of the two `AppState`s it was just told about.
        """

        async def report(state: AppState, line: str, note: str | None) -> None:
            current = self._current_adding()
            if current is None:
                return
            await self._publish(self._replace_finale(adding=replace(current, line=line, note=note)))

        return report

    async def _run_wiring_for_add(
        self,
        app: CatalogApp,
        install: InstallState,
        record_diagnostics: Callable[[str], None],
        *,
        also_ids: tuple[str, ...] = (),
        wire_ids: tuple[str, ...] = (),
    ) -> None:
        """Run the wiring steps about each id in `wire_ids` (`app.id` alone
        when not given), then fold `app.id` (and every id in `also_ids` -
        the companion(s) this same add brought along, a fresh Gluetun
        beside a fresh qBittorrent) into `apps`.

        Shared by a fresh add and a resumed one - the wiring runner is
        idempotent (look-before-write), so re-running it for an app whose
        wiring already fully succeeded on an earlier, interrupted attempt
        is a repeat, not a risk. `also_ids` is `()` for a plain reconnect
        and for an add whose companion already existed (the CONTRACT FIX
        remove-then-recreate case) - that app is already `done` in `apps`.
        `wire_ids` is what "Add your VPN" and Change VPN wire instead of
        `app.id`: the mover(s) whose download-client host just changed,
        never Gluetun itself, which nothing points at directly.
        """
        ids_to_wire = wire_ids or (app.id,)
        for wire_id in ids_to_wire:
            wiring_rows: dict[int, WiringStep] = {}

            def collect_wiring_step(
                step: WiringStep, _rows: dict[int, WiringStep] = wiring_rows
            ) -> None:
                if step.technical:
                    record_diagnostics(self._redact(step.technical, install))
                _rows[step.index] = replace(step, technical=None)
                current = self._current_adding()
                if current is not None:
                    ordered_steps = tuple(_rows[index] for index in sorted(_rows))
                    self._emit(self._replace_finale(adding=replace(current, wiring=ordered_steps)))

            try:
                await self._wiring.run(install, collect_wiring_step, only_app=wire_id)
            except Exception:
                # A wiring problem is not an add failure - the app is already
                # up and done, so this never turns a successful add into one
                # that looks failed.
                logger.exception("wiring runner raised during an add; continuing anyway")

            wiring_steps = tuple(wiring_rows[index] for index in sorted(wiring_rows))
            failed_lines = tuple(step.line for step in wiring_steps if step.state == "error")
            self._emit(
                self._replace_finale(
                    adding=self._current_adding(),
                    wiring_gaps=self._gaps_with(wire_id, failed_lines),
                )
            )

        progresses_by_id = {progress.app_id: progress for progress in self._snapshot.apps}
        for finished_app in apps_in_order((*also_ids, app.id)):
            progresses_by_id[finished_app.id] = AppProgress(
                app_id=finished_app.id,
                name=finished_app.name,
                state="done",
                chip=STATUS_CHIP_DONE,
                line=app_line_done(finished_app.name),
                note=None,
                port=finished_app.port,
            )
        ordered_ids = tuple(catalog_app.id for catalog_app in apps_in_order(progresses_by_id))
        new_apps = tuple(progresses_by_id[app_id] for app_id in ordered_ids)

        await self._publish(self._replace_finale(adding=None, apps=new_apps))

    def _drop_replaced_link(self) -> None:
        """Remove the link card a successful existing-Plex connect chose
        to replace - never inside `_run_wiring_for_add`, which a plain
        reconnect also calls, and only reached once the bring-up above has
        already succeeded, so a failed connect never touches it. No await
        lands between the load and the two saves, so a second reader can
        never see the link gone but the record still pointing at it (or
        the reverse).
        """
        config_dir = self._settings.config_dir
        record = load_existing_plex(config_dir)
        if record is None or record.replaces_link is None:
            return
        try:
            kept = [link for link in load_links(config_dir) if link.id != record.replaces_link]
            save_links(config_dir, kept)
            save_existing_plex(config_dir, replace(record, replaces_link=None))
        except OSError as error:
            logger.warning("could not remove the replaced link card: %s", error)

    async def _run_reconnect(self, app: CatalogApp, install: InstallState) -> None:
        record_diagnostics = self._diagnostics_recorder()
        try:
            await self._run_wiring_for_add(app, install, record_diagnostics)
        except Exception as error:  # the background task must never die silently
            logger.exception("reconnect crashed unexpectedly")
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            await self._fail_add(
                app,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
                record_diagnostics,
            )

    # --- Moving qBittorrent: shared by "Add your VPN" and Change VPN ------

    async def _take_down(self, movers: tuple[CatalogApp, ...]) -> Failure | None:
        """Stop, then force-remove, each of `movers` - in REVERSE catalog
        order, so a rider's own container goes before the network
        namespace it shares disappears with its companion's.

        A failed stop is logged and ignored - the removal that follows
        forces whatever is left, the same way Cancel's own removal always
        has. A failed removal is a real failure: nothing about the VPN is
        touched while an old container might still be holding the port
        (8080) it needs to publish.
        """
        for mover in reversed(movers):
            stop_result = await self._engine.stop_container(mover.id)
            if not stop_result.ok:
                logger.warning(
                    "could not gently stop %s before moving it: %s", mover.id, stop_result.detail
                )
            remove_result = await self._engine.remove_container(mover.id)
            if not remove_result.ok:
                headline, what_to_do = _split_failure_text(failure_compose_failed(mover.name))
                return Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=remove_result.detail or f"could not remove {mover.id!r}",
                )
        return None

    async def _run_change_vpn(self) -> None:
        """Re-enter Change VPN from scratch - a fresh start, a retry, or a
        resume all call this the same, zero-argument way: everything it
        needs (the install, which app, which rider(s)) is read fresh from
        disk and from the persisted `adding` record, never carried in from
        a caller that might hand it something stale after a restart.
        """
        record_diagnostics = self._diagnostics_recorder()
        app = get_app(VPN_APP_ID)
        install = load_state(self._settings.config_dir)
        if install is None:
            logger.error("change VPN run started with no install state on disk")
            current = self._current_adding()
            if current is not None:
                headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
                failure = Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical="no install state found for change_vpn",
                )
                await self._publish(
                    self._replace_finale(
                        adding=replace(
                            current, state="error", line=app_line_error(app.name), failure=failure
                        )
                    )
                )
            return
        try:
            await self._run_change_vpn_steps(app, install, record_diagnostics)
        except Exception as error:  # the background task must never die silently
            logger.exception("change VPN run crashed unexpectedly")
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            await self._fail_add(
                app,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
                record_diagnostics,
            )

    async def _run_change_vpn_steps(
        self, app: CatalogApp, install: InstallState, record_diagnostics: Callable[[str], None]
    ) -> None:
        current = self._current_adding()
        movers = apps_in_order(current.moves) if current is not None else ()

        docker_status = await self._engine.status()
        if not docker_status.connected:
            await self._fail_add(
                app,
                install,
                _docker_unreachable_failure(docker_status.detail or "Docker did not answer"),
                record_diagnostics,
            )
            return

        answers = load_answers(self._settings.config_dir)
        vpn_failure = _vpn_settings_failure(install.app_ids, answers)
        if vpn_failure is not None:
            await self._fail_add(app, install, vpn_failure, record_diagnostics)
            return

        plan = self._stack_plan(install)
        compose_path = write_compose(self._settings, plan)

        self_id = await self._engine.self_container_id()
        if self_id is None:
            await self._fail_add(
                app,
                install,
                _docker_unreachable_failure("could not identify Marrquee's own container"),
                record_diagnostics,
            )
            return

        if movers:
            take_down_failure = await self._take_down(movers)
            if take_down_failure is not None:
                await self._fail_add(app, install, take_down_failure, record_diagnostics)
                return

        # Gluetun only reads its secrets when it starts - a login-only
        # change never shows up in compose.yaml, so `recreate=True` would
        # leave it running the OLD login forever. Remove-then-create is
        # the only honest recreate; the rider(s) are already gone by now,
        # so there is no container whose network points at a dead Gluetun.
        remove_result = await self._engine.remove_container(app.id)
        if not remove_result.ok:
            await self._fail_add(
                app,
                install,
                _docker_unreachable_failure(remove_result.detail or f"could not remove {app.id!r}"),
                record_diagnostics,
            )
            return

        report = self._add_reporter()
        failure = await self._bring_up_app(
            app, install, plan.storage_root, compose_path, plan.network, self_id, report
        )
        if failure is not None:
            await self._fail_add(app, install, failure, record_diagnostics)
            return

        for mover in movers:
            mover_failure = await self._bring_up_app(
                mover, install, plan.storage_root, compose_path, plan.network, self_id, report
            )
            if mover_failure is not None:
                await self._fail_add(app, install, mover_failure, record_diagnostics)
                return
            if mover.id in pending_app_ids(load_login(self._settings.config_dir), install.app_ids):
                await self._put_login(mover, install, record_diagnostics=record_diagnostics)

        current = self._current_adding()
        if current is not None:
            self._emit(
                self._replace_finale(
                    adding=replace(current, state="wiring", line=hub_line_connecting(app.name))
                )
            )

        mover_ids = tuple(mover.id for mover in movers)
        await self._run_wiring_for_add(app, install, record_diagnostics, wire_ids=mover_ids)

    async def _run_restore(self) -> None:
        """Bring the mover(s) a failed move stopped back up on their own
        network - "Keep running without VPN". Zero-argument for the same
        reason as `_run_change_vpn`: a fresh start, a retry and a resume
        all call it the same way.
        """
        record_diagnostics = self._diagnostics_recorder()
        current = self._current_adding()
        movers = apps_in_order(current.moves) if current is not None else ()
        install = load_state(self._settings.config_dir)
        if install is None or not movers:
            logger.error("restore run started with no install state or no movers to restore")
            if current is not None:
                lead_name = get_app(current.app_id).name
                headline, what_to_do = _split_failure_text(failure_compose_failed(lead_name))
                failure = Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical="no install state or movers found for restore",
                )
                await self._publish(
                    self._replace_finale(
                        adding=replace(
                            current, state="error", line=app_line_error(lead_name), failure=failure
                        )
                    )
                )
            return
        try:
            await self._run_restore_steps(movers, install, record_diagnostics)
        except Exception as error:  # the background task must never die silently
            logger.exception("restore run crashed unexpectedly")
            lead = movers[0]
            headline, what_to_do = _split_failure_text(failure_compose_failed(lead.name))
            await self._fail_add(
                lead,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
                record_diagnostics,
            )

    async def _run_restore_steps(
        self,
        movers: tuple[CatalogApp, ...],
        install: InstallState,
        record_diagnostics: Callable[[str], None],
    ) -> None:
        lead = movers[0]

        docker_status = await self._engine.status()
        if not docker_status.connected:
            await self._fail_add(
                lead,
                install,
                _docker_unreachable_failure(docker_status.detail or "Docker did not answer"),
                record_diagnostics,
            )
            return

        plan = self._stack_plan(install)
        compose_path = write_compose(self._settings, plan)

        self_id = await self._engine.self_container_id()
        if self_id is None:
            await self._fail_add(
                lead,
                install,
                _docker_unreachable_failure("could not identify Marrquee's own container"),
                record_diagnostics,
            )
            return

        report = self._add_reporter()
        for mover in movers:
            failure = await self._bring_up_app(
                mover, install, plan.storage_root, compose_path, plan.network, self_id, report
            )
            if failure is not None:
                await self._fail_add(lead, install, failure, record_diagnostics)
                return
            if mover.id in pending_app_ids(load_login(self._settings.config_dir), install.app_ids):
                await self._put_login(mover, install, record_diagnostics=record_diagnostics)

        current = self._current_adding()
        if current is not None:
            self._emit(
                self._replace_finale(
                    adding=replace(current, state="wiring", line=hub_line_connecting(lead.name))
                )
            )

        mover_ids = tuple(mover.id for mover in movers)
        await self._run_wiring_for_add(lead, install, record_diagnostics, wire_ids=mover_ids)

    async def _fail_add(
        self,
        app: CatalogApp,
        install: InstallState,
        failure: Failure,
        record_diagnostics: Callable[[str], None],
    ) -> None:
        redacted = replace(failure, technical=self._redact(failure.technical, install))
        record_diagnostics(f"{redacted.code}\n{redacted.technical}")
        logger.error("add failed (%s): %s", redacted.code, redacted.technical)
        current = self._current_adding()
        if current is None:
            return
        updated = replace(current, state="error", line=app_line_error(app.name), failure=redacted)
        await self._publish(self._replace_finale(adding=updated))

    async def _fail(
        self,
        run_id: str,
        started_at: str,
        progresses: list[AppProgress],
        install: InstallState,
        failure: Failure,
    ) -> None:
        redacted = replace(failure, technical=self._redact(failure.technical, install))
        self._diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
        self._diagnostics_file.write_text(f"{redacted.code}\n{redacted.technical}\n")
        logger.error("deploy failed (%s): %s", redacted.code, redacted.technical)
        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="error",
                apps=tuple(_stopped_progress(app) for app in progresses),
                headline=redacted.headline,
                detail=redacted.what_to_do,
                failure=redacted,
                started_at=started_at,
                finished_at=_now_iso(),
                wiring=(),
            )
        )

    def _append_diagnostics(self, text: str) -> None:
        self._diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
        with self._diagnostics_file.open("a", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else f"{text}\n")

    def _redact(self, text: str, install: InstallState) -> str:
        """The one place every diagnostics write, snapshot failure and log
        line in this class goes through - keys first, then the saved login
        password, then the saved VPN login, then the owner's Plex account
        token AND the owner's own, already-running Plex's server token,
        then Jellyfin's own saved API key (all read fresh, never cached,
        and never passed in by a caller that might get them stale).
        """
        saved = load_login(self._settings.config_dir).login
        passwords = (saved.password,) if saved is not None else ()
        vpn_answers = load_answers(self._settings.config_dir).get(VPN_APP_ID, {})
        plex_account = load_plex_account(self._settings.config_dir)
        existing_plex = load_existing_plex(self._settings.config_dir)
        plex_values = tuple(
            token
            for token in (
                plex_account.token if plex_account is not None else None,
                existing_plex.token if existing_plex is not None else None,
            )
            if token
        )
        jellyfin_record = load_jellyfin(self._settings.config_dir)
        jellyfin_values = (jellyfin_record.api_key,) if jellyfin_record is not None else ()
        return _redact_secrets(
            text,
            install.api_keys,
            passwords=passwords,
            vpn_values=secret_values(vpn_answers),
            plex_values=plex_values,
            jellyfin_values=jellyfin_values,
        )

    # --- Putting the saved login on one app --------------------------------

    async def _put_login(
        self,
        app: CatalogApp,
        install: InstallState,
        *,
        record_diagnostics: Callable[[str], None] | None = None,
    ) -> bool:
        """Put the saved login on `app` right after it's ready - the one
        call site both a full deploy and an add share.

        Never raises. `False`, with no call to the applier at all, when
        there is nothing to put (`app.login_kind == "none"`) or nothing
        saved yet - the safe default that never locks an app out of a
        login it was never given. A failure is written to diagnostics
        through `record_diagnostics` when given (an add's own
        first-write-replaces recorder), or `_append_diagnostics` otherwise
        (a full deploy's own diagnostics file, already cleared by `start()`).
        """
        if app.login_kind == "none":
            return False
        record = load_login(self._settings.config_dir)
        if record.login is None:
            return False

        result = await self._login.apply(app, install, record.login)
        if result.ok:
            record_applied(self._settings.config_dir, app.id, record.login.generation)
            return True

        write = record_diagnostics if record_diagnostics is not None else self._append_diagnostics
        technical = result.technical or f"{app.id}: the saved login was not accepted"
        redacted = self._redact(technical, install)
        write(redacted)
        logger.error("could not put the saved login on %s: %s", app.id, redacted)
        return False

    # --- The login run: choose, change and retry all share this ------------

    def _login_reporter(self) -> Callable[[AppState, str, str | None], Awaitable[None]]:
        """`_bring_up_app`'s own `report` callback for the login run's phase
        2 - it only ever updates `login_progress`, since the login run has
        no `DeploySnapshot` of its own to publish into.
        """

        async def report(state: AppState, line: str, note: str | None) -> None:
            self._login_progress = line

        return report

    async def _run_login(
        self, install: InstallState, login: SavedLogin, target_ids: tuple[str, ...]
    ) -> None:
        record_diagnostics = self._diagnostics_recorder()
        try:
            await self._run_login_steps(install, login, target_ids, record_diagnostics)
        except Exception as error:  # the background task must never die silently
            logger.exception("login run crashed unexpectedly")
            record_diagnostics(
                self._redact(f"login run crashed: {type(error).__name__}: {error}", install)
            )
        finally:
            self._login_progress = None

    async def _run_login_steps(
        self,
        install: InstallState,
        login: SavedLogin,
        target_ids: tuple[str, ...],
        record_diagnostics: Callable[[str], None],
    ) -> None:
        """Phase 1 puts the login on every target through its own API.
        Phase 2 recreates - one at a time - only the apps that accepted it,
        so an app that refused the login is never left asking for one it
        doesn't have.
        """
        targets = apps_in_order(target_ids)
        accepted: list[CatalogApp] = []
        for app in targets:
            self._login_progress = hub_login_line_putting(app.name)
            result = await self._login.apply(app, install, login)
            if result.ok:
                accepted.append(app)
            else:
                technical = result.technical or f"{app.id}: the saved login was not accepted"
                redacted = self._redact(technical, install)
                record_diagnostics(redacted)
                logger.error("login run: %s did not accept the login: %s", app.id, redacted)

        if not accepted:
            return

        # qBittorrent's login is live the instant phase 1's POST succeeds -
        # it never needs (and must never get) the recreate phase 2 exists
        # for: recreating it would also orphan whatever rides its network
        # for no reason. Any kind other than "arr" that accepted the login
        # is recorded applied right away instead of joining that loop.
        immediate = [app for app in accepted if app.login_kind != "arr"]
        recreate_targets = [app for app in accepted if app.login_kind == "arr"]
        for app in immediate:
            record_applied(self._settings.config_dir, app.id, login.generation)

        if not recreate_targets:
            return

        docker_status = await self._engine.status()
        if not docker_status.connected:
            record_diagnostics(
                self._redact(
                    "login run: Docker did not answer "
                    f"({docker_status.detail or 'no further detail'})",
                    install,
                )
            )
            return

        answers = load_answers(self._settings.config_dir)
        vpn_failure = _vpn_settings_failure(install.app_ids, answers)
        if vpn_failure is not None:
            # Not "login run crashed": the apps that DID accept the login
            # are still up and done, so this is a diagnostics line, never a
            # Failure the login run has no snapshot to carry anyway.
            record_diagnostics(self._redact(vpn_failure.technical, install))
            logger.error("login run: %s", vpn_failure.technical)
            return
        plan = self._stack_plan(install)
        compose_path = write_compose(self._settings, plan)
        self_id = await self._engine.self_container_id()
        if self_id is None:
            record_diagnostics("login run: could not identify Marrquee's own container")
            return

        report = self._login_reporter()
        for app in recreate_targets:
            self._login_progress = hub_login_line_restarting(app.name)
            failure = await self._bring_up_app(
                app,
                install,
                plan.storage_root,
                compose_path,
                plan.network,
                self_id,
                report,
                recreate=True,
            )
            if failure is None:
                record_applied(self._settings.config_dir, app.id, login.generation)
            else:
                redacted = self._redact(failure.technical, install)
                record_diagnostics(redacted)
                logger.error("login run: %s could not be restarted: %s", app.id, redacted)


def read_last_failure(config_dir: Path) -> str | None:
    """The diagnostics file's own text, for the Diagnostics page's "Last
    problem" section - or `None` when there is nothing worth showing.

    `None` covers a missing file, one that can't be read, and one that's
    empty or whitespace-only - the page has exactly one wording for
    "nothing has gone wrong", and this is the single place that decides
    which of the two frames it's in. Reads with `errors="replace"` so a
    stray non-UTF-8 byte in captured output can never turn "show the
    problem" into a 500.
    """
    path = config_dir / _DIAGNOSTICS_FILE_NAME
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return text if text.strip() else None


def _running_snapshot(
    run_id: str, started_at: str, progresses: list[AppProgress], headline: str
) -> DeploySnapshot:
    return DeploySnapshot(
        run_id=run_id,
        phase="running",
        apps=tuple(progresses),
        headline=headline,
        detail=None,
        failure=None,
        started_at=started_at,
        finished_at=None,
        wiring=(),
    )


def _resting_snapshot() -> DeploySnapshot:
    return DeploySnapshot(
        run_id="",
        phase="ready",
        apps=(),
        headline=PHASE_HEADLINE_READY,
        detail=None,
        failure=None,
        started_at=None,
        finished_at=None,
        wiring=(),
    )


def _waiting_progress(app: CatalogApp) -> AppProgress:
    return AppProgress(
        app_id=app.id,
        name=app.name,
        state="waiting",
        chip=STATUS_CHIP_WAITING,
        line=STATUS_CHIP_WAITING,
        note=None,
        port=app.port,
    )


def _stopped_progress(app: AppProgress) -> AppProgress:
    """The app that was `starting` when the deploy failed, told the truth.

    `waiting` and `done` entries are untouched - only the one app the run
    was actually waiting on when it gave up ever spent the failure sitting
    there mid-boot, gold and spinning, above a panel that just said it
    failed.
    """
    if app.state != "starting":
        return app
    return replace(
        app, state="error", chip=STATUS_CHIP_ERROR, line=app_line_error(app.name), note=None
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _split_failure_text(message: str) -> tuple[str, str]:
    """Split one of words.py's "what happened. what to do next." messages.

    Every failure message in words.py reads as exactly that shape - one
    sentence naming the problem, then one or more naming the fix. Splitting
    on the first sentence boundary keeps `Failure.headline` and
    `Failure.what_to_do` genuinely distinct without inventing wording beyond
    what is already approved.
    """
    headline, separator, what_to_do = message.partition(". ")
    if not separator:
        return message, message
    return f"{headline}.", what_to_do


def _docker_unreachable_failure(detail: str) -> Failure:
    headline, what_to_do = _split_failure_text(FAILURE_DOCKER_UNREACHABLE)
    return Failure(
        code="docker_unreachable", headline=headline, what_to_do=what_to_do, technical=detail
    )


_STORAGE_REFUSAL_WORDS: dict[str, Callable[[str], str]] = {
    "empty": refusal_path_missing,
    "not_absolute": refusal_path_missing,
    "system_path": refusal_system_path,
    "missing": refusal_path_missing,
    "not_a_folder": refusal_not_a_folder,
    "not_writable": refusal_not_writable,
    "not_shared": refusal_not_shared,
}


def _storage_failure(check: StorageCheck, path: str) -> Failure:
    wording = _STORAGE_REFUSAL_WORDS.get(check.reason or "missing", refusal_path_missing)
    headline, what_to_do = _split_failure_text(wording(path))
    return Failure(
        code="storage_refused",
        headline=headline,
        what_to_do=what_to_do,
        technical=f"reason={check.reason}, detail={check.detail}",
    )


def _freshness_failure(freshness: FreshnessCheck, path: str) -> Failure:
    headline, what_to_do = _split_failure_text(refusal_populated_target(path))
    return Failure(
        code="storage_refused",
        headline=headline,
        what_to_do=what_to_do,
        technical=f"occupied={freshness.occupied}",
    )


def _looks_like_port_conflict(output: str) -> bool:
    lowered = output.lower()
    return "already allocated" in lowered or "address already in use" in lowered


def _compose_failure(
    app: CatalogApp,
    downloading: bool,
    result: ComposeResult,
    riders: tuple[CatalogApp, ...] = (),
) -> Failure:
    if downloading:
        headline, what_to_do = _split_failure_text(failure_get_failed(app.name))
        return Failure(
            code="image_download_failed",
            headline=headline,
            what_to_do=what_to_do,
            technical=result.output,
        )
    # Gluetun's own port is never published (`_vpn_service_plan` gives it
    # no `ports:` of its own) - a real conflict on 8000 could only ever be
    # its rider's own published port, so the failure names THAT app and
    # port, never Gluetun's.
    named_app = riders[0] if riders else app
    if named_app.port is not None and _looks_like_port_conflict(result.output):
        headline, what_to_do = _split_failure_text(
            failure_port_in_use(named_app.name, named_app.port)
        )
        return Failure(
            code="port_in_use", headline=headline, what_to_do=what_to_do, technical=result.output
        )
    headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
    return Failure(
        code="compose_failed", headline=headline, what_to_do=what_to_do, technical=result.output
    )


def _vpn_settings_failure(
    app_ids: Iterable[str], answers: Mapping[str, Mapping[str, str]]
) -> Failure | None:
    """`None` when Gluetun isn't installed, or its saved answers are still
    acceptable; otherwise a `vpn_settings_refused` `Failure` that keeps
    `build_stack_plan` from ever being called with answers it would only
    raise on - compose is never written for a run this catches.
    """
    if VPN_APP_ID not in set(app_ids):
        return None
    check = check_vpn_answers(answers.get(VPN_APP_ID, {}))
    if check.ok:
        return None
    provider = find_provider(check.answers.get("provider", ""))
    company = provider.label if provider is not None else "VPN"
    headline, what_to_do = _split_failure_text(failure_vpn_settings_refused(company))
    return Failure(
        code="vpn_settings_refused",
        headline=headline,
        what_to_do=what_to_do,
        technical="the saved VPN answers are missing or invalid",
    )


def _vpn_verdict_failure(code: FailureCode, message: str, logs: str) -> Failure:
    headline, what_to_do = _split_failure_text(message)
    return Failure(code=code, headline=headline, what_to_do=what_to_do, technical=logs)


def _redact_secrets(
    text: str,
    api_keys: Mapping[str, str],
    passwords: Iterable[str] = (),
    vpn_values: Iterable[str] = (),
    plex_values: Iterable[str] = (),
    jellyfin_values: Iterable[str] = (),
) -> str:
    """Replace every known API key, then every known password, then every
    known VPN credential, then the owner's Plex account token, then
    Jellyfin's own saved API key, with a placeholder before text reaches a
    log line, a diagnostics file or a snapshot's failure detail - and, last
    of all, sweep for a claim code's own shape, which this process may never
    have loaded as a value at all (an already-expired one, echoed back in a
    container's own logs).

    Docker or an app's own error output could echo back an environment
    value we set ourselves - redacting by value here is what keeps "a secret
    never appears in a log or a diagnostics file" true even if a future log
    line surprises us. Each category is redacted after the last so a value
    that happened to also look like an earlier category's still comes out
    right either way.
    """
    redacted = text
    for key in api_keys.values():
        if key:
            redacted = redacted.replace(key, _REDACTED_PLACEHOLDER)
    for password in passwords:
        if password:
            redacted = redacted.replace(password, _REDACTED_PASSWORD_PLACEHOLDER)
    for value in vpn_values:
        if value:
            redacted = redacted.replace(value, _REDACTED_VPN_PLACEHOLDER)
    for value in plex_values:
        if value:
            redacted = redacted.replace(value, _REDACTED_PLEX_TOKEN_PLACEHOLDER)
    for value in jellyfin_values:
        if value:
            redacted = redacted.replace(value, _REDACTED_JELLYFIN_PLACEHOLDER)
    return _PLEX_CLAIM_PATTERN.sub(_REDACTED_PLEX_CLAIM_PLACEHOLDER, redacted)


def _wiring_finale_detail(steps: tuple[WiringStep, ...]) -> str | None:
    """`None` on a clean run; otherwise every failed step's own line, in
    step order - the finale stays green either way (a wiring problem is
    never a deploy failure), but a failed connection still gets named.
    """
    failed_lines = tuple(step.line for step in steps if step.state == "error")
    return wiring_finale_note(failed_lines) if failed_lines else None


# --- deploy.json round-tripping: reload-on-construction, resume, persistence -


def _snapshot_from_payload(payload: object) -> DeploySnapshot:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")

    apps = tuple(_app_progress_from_payload(item) for item in _require_list(payload.get("apps")))
    wiring = tuple(
        _wiring_step_from_payload(item) for item in _require_list(payload.get("wiring", []))
    )
    failure_payload = payload.get("failure")
    failure = _failure_from_payload(failure_payload) if isinstance(failure_payload, dict) else None
    adding_payload = payload.get("adding")
    adding = _app_add_from_payload(adding_payload) if isinstance(adding_payload, dict) else None
    wiring_gaps = tuple(
        _wiring_gap_from_payload(item) for item in _require_list(payload.get("wiring_gaps", []))
    )

    return DeploySnapshot(
        run_id=_require_str(payload.get("run_id")),
        phase=cast(
            DeployPhase,
            _require_choice(
                payload.get("phase"), ("ready", "running", "wiring", "finale", "error")
            ),
        ),
        apps=apps,
        headline=_require_str(payload.get("headline")),
        detail=_require_optional_str(payload.get("detail")),
        failure=failure,
        started_at=_require_optional_str(payload.get("started_at")),
        finished_at=_require_optional_str(payload.get("finished_at")),
        wiring=wiring,
        adding=adding,
        wiring_gaps=wiring_gaps,
    )


def _app_progress_from_payload(payload: object) -> AppProgress:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")
    return AppProgress(
        app_id=_require_str(payload.get("app_id")),
        name=_require_str(payload.get("name")),
        state=cast(
            AppState,
            _require_choice(payload.get("state"), ("waiting", "starting", "done", "error")),
        ),
        chip=_require_str(payload.get("chip")),
        line=_require_str(payload.get("line")),
        note=_require_optional_str(payload.get("note")),
        port=None if payload.get("port") is None else _require_int(payload.get("port")),
    )


def _failure_from_payload(payload: dict[str, object]) -> Failure:
    return Failure(
        code=cast(
            FailureCode,
            _require_choice(
                payload.get("code"),
                (
                    "docker_unreachable",
                    "storage_refused",
                    "name_clash",
                    "image_download_failed",
                    "compose_failed",
                    "never_became_ready",
                    "port_in_use",
                    "vpn_refused",
                    "vpn_settings_refused",
                    "vpn_not_connected",
                    "vpn_no_tun",
                    "plex_sign_in_needed",
                    "plex_not_claimed",
                    "plex_port_taken",
                    "jellyfin_port_taken",
                    "jellyfin_not_ours",
                    "jellyfin_setup_refused",
                    "existing_plex_unreachable",
                    "seerr_not_ours",
                    "seerr_setup_refused",
                ),
            ),
        ),
        headline=_require_str(payload.get("headline")),
        what_to_do=_require_str(payload.get("what_to_do")),
        technical=_require_str(payload.get("technical")),
    )


def _wiring_step_from_payload(payload: object) -> WiringStep:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")
    involved = payload.get("involved", [])
    if not isinstance(involved, list) or not all(isinstance(item, str) for item in involved):
        raise TypeError(f"expected a list of strings, got {involved!r}")
    return WiringStep(
        index=_require_int(payload.get("index")),
        total=_require_int(payload.get("total")),
        key=_require_str(payload.get("key")),
        line=_require_str(payload.get("line")),
        state=cast(
            WiringStepState,
            _require_choice(payload.get("state"), ("running", "done", "skipped", "error")),
        ),
        chip=_require_str(payload.get("chip")),
        note=_require_optional_str(payload.get("note")),
        technical=_require_optional_str(payload.get("technical")),
        involved=tuple(involved),
    )


def _app_add_from_payload(payload: dict[str, object]) -> AppAdd:
    wiring = tuple(
        _wiring_step_from_payload(item) for item in _require_list(payload.get("wiring", []))
    )
    failure_payload = payload.get("failure")
    failure = _failure_from_payload(failure_payload) if isinstance(failure_payload, dict) else None
    with_apps_payload = payload.get("with_apps", [])
    if not isinstance(with_apps_payload, list) or not all(
        isinstance(item, str) for item in with_apps_payload
    ):
        raise TypeError(f"expected a list of strings, got {with_apps_payload!r}")
    moves_payload = payload.get("moves", [])
    if not isinstance(moves_payload, list) or not all(
        isinstance(item, str) for item in moves_payload
    ):
        raise TypeError(f"expected a list of strings, got {moves_payload!r}")
    return AppAdd(
        app_id=_require_str(payload.get("app_id")),
        purpose=cast(
            AddPurpose,
            _require_choice(payload.get("purpose"), ("add", "reconnect", "change_vpn", "restore")),
        ),
        state=cast(
            AddState, _require_choice(payload.get("state"), ("starting", "wiring", "error"))
        ),
        line=_require_str(payload.get("line")),
        note=_require_optional_str(payload.get("note")),
        failure=failure,
        wiring=wiring,
        compose_ran=_require_bool(payload.get("compose_ran")),
        started_at=_require_str(payload.get("started_at")),
        with_apps=tuple(with_apps_payload),
        moves=tuple(moves_payload),
    )


def _wiring_gap_from_payload(payload: object) -> WiringGap:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")
    failed_lines = payload.get("failed_lines", [])
    if not isinstance(failed_lines, list) or not all(
        isinstance(item, str) for item in failed_lines
    ):
        raise TypeError(f"expected a list of strings, got {failed_lines!r}")
    return WiringGap(app_id=_require_str(payload.get("app_id")), failed_lines=tuple(failed_lines))


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected a bool, got {value!r}")
    return value


def _require_optional_str(value: object) -> str | None:
    return None if value is None else _require_str(value)


def _require_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected a whole number, got {value!r}")
    return value


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"expected a list, got {value!r}")
    return value


def _require_choice(value: object, allowed: tuple[str, ...]) -> str:
    """A string that is one of `allowed`, or a ValueError.

    Callers `cast()` the result to whichever `Literal` type `allowed`
    represents - the runtime membership check just above the cast is what
    makes that cast honest rather than a bare assertion.
    """
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"expected one of {allowed}, got {value!r}")
    return value
